"""NiceGUI test UI for SpiriCamera.

Frames are served over a plain HTTP route and pulled by an
``ui.interactive_image``, rather than pushed as base64 data URLs over
the websocket.  Two reasons, both of which showed up as visible
artefacts:

* ``ui.image`` wraps Quasar's ``QImg``, which cross-fades every time its
  source changes.  At video framerates that fade never finishes, so the
  picture looks permanently dimmed and pulses at a constant rate.
  ``ui.interactive_image`` has no transition.
* A data URL is pushed to every client for every frame whether or not
  the browser can keep up.  Pulling instead lets the component skip
  frames it cannot load in time, which is what makes it adapt to the
  available bandwidth.

See https://nicegui.io/documentation/interactive_image and NiceGUI's
``examples/opencv_webcam``.

Every control on this page binds two-way with ``bind_value``, including
the fields the camera normally fills in for itself.  This is deliberate:
no SpiriSynq property is truly read-only, a remote peer can write any of
them already, and this page exists to debug that.  Use ``bind_value``,
not ``bind_value_from``, unless a field genuinely has nowhere to write
back to.  Two things here qualify: the bandwidth meter, which is a
measurement taken at the HTTP route, and the frame tags, which are read
out of a frame that has already been encoded and sent.  Both have a
writable counterpart nearby — the camera's own settings, and Extra Tags.
"""

from __future__ import annotations

import base64
import re
import threading
import time
from collections import deque

from fastapi import Response
from nicegui import app, ui

from SpiriCamera import Camera, exif
from SpiriCamera.main import get_settings
from SpiriCamera.overlay import (
    ANCHORS,
    DEFAULT_FONT,
    DEFAULT_FONT_PATH,
    HudWidget,
    camera_metrics_widget,
    declared_objects,
    tiger_widget,
)
from SpiriCamera.sources.testimage import raster_test_images, test_images
from SpiriCamera.sources.v4l import list_devices

#: Route the browser pulls frames from.
FRAME_ROUTE = '/camera/frame'

#: Seconds of history the bandwidth meter averages over.
METER_WINDOW = 3.0


class RateMeter:
    """Rolling-window meter for what the frame route actually serves.

    Measured at the HTTP route, so it reports what the browsers are
    really pulling rather than what the camera hoped to publish.  Shared
    by every client, matching the one-camera-per-process model.

    This is the documented exception to the two-way binding rule in the
    module docstring: it is a derived measurement with nowhere to write
    back to, so the UI reads it and nothing more.
    """

    def __init__(self, window: float = METER_WINDOW) -> None:
        """Create a meter.

        Parameters
        ----------
        window : float
            Seconds of history to average over.
        """
        self._window = window
        self._samples: deque[tuple[float, int, float]] = deque()
        self._newest_capture = 0.0
        self._lock = threading.Lock()

    def record(self, size: int, captured: float = 0.0) -> None:
        """Record one served frame.

        Parameters
        ----------
        size : int
            Size of the served frame in bytes.
        captured : float
            Unix timestamp the frame was captured at, from its own EXIF.
            Zero for an untagged frame, whose age is unknowable.
        """
        now = time.monotonic()
        # Clamped: a camera whose clock is ahead of ours must not produce
        # a frame from the future.
        age = max(0.0, time.time() - captured) if captured else 0.0
        with self._lock:
            self._samples.append((now, size, age))
            self._newest_capture = max(self._newest_capture, captured)
            self._prune(now)

    def rates(self) -> tuple[float, float]:
        """Return the current rates.

        Returns
        -------
        tuple[float, float]
            Frames per second and bytes per second over the window.
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._samples) < 2:
                return 0.0, 0.0
            span = now - self._samples[0][0]
            total = sum(size for _, size, _ in self._samples)
            count = len(self._samples)
        if span <= 0:
            return 0.0, 0.0
        return count / span, total / span

    def mean_age(self) -> float:
        """Return the mean capture-to-serve age over the window.

        Averaged rather than sampled.  A frame's age sweeps from zero to
        a full frame interval as it waits to be fetched, so reading it at
        one arbitrary instant measures the phase between the capture loop
        and whatever clock did the reading — a number that slides and
        wraps and says nothing about latency.

        Returns
        -------
        float
            Seconds, or ``0.0`` if no tagged frame has been served in the
            window.
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            ages = [age for _, _, age in self._samples if age]
        if not ages:
            return 0.0
        return sum(ages) / len(ages)

    def age(self) -> float:
        """Return the age to report for the frame currently on screen.

        The larger of two readings of the same thing, because a displayed
        age must never claim the picture is fresher than it is:

        * the mean over the window, which is the stable answer while
          frames are flowing and the only one that does not slide about;
        * the true age of the newest frame seen, which is what the mean
          decays away from once frames stop arriving.  A camera that
          stopped ten minutes ago is still showing a ten-minute-old
          picture, and that is the useful thing to say about it.

        Returns
        -------
        float
            Seconds, or ``0.0`` if no tagged frame has ever been served.
        """
        with self._lock:
            newest = self._newest_capture
        if not newest:
            return 0.0
        return max(self.mean_age(), max(0.0, time.time() - newest))

    def _prune(self, now: float) -> None:
        """Drop samples older than the window; caller holds the lock."""
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()


#: Meter for the frame route, shared across clients.
frame_meter = RateMeter()

#: 1x1 black PNG, served before the first frame exists.
PLACEHOLDER = Response(
    content=base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk'
        'YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
    ),
    media_type='image/png',
    headers={'Cache-Control': 'no-store'},
)

_camera: Camera | None = None


def _make_authoritive(source_str: str) -> Camera:
    """Build a fresh authoritative camera pointed at ``source_str``.

    Encode settings are read from the process's own configuration, not
    carried over from whatever camera this replaces -- an authoritative
    camera is this process actually running a device, and that device's
    settings come from how the process was configured, not from
    whatever a mirror happened to be showing a moment ago.

    Parameters
    ----------
    source_str : str
        Source URL or device path to open.

    Returns
    -------
    Camera
        A new, authoritative camera.
    """
    settings = get_settings()
    return Camera(
        source_str,
        quality=settings.quality,
        max_width=settings.frame_width,
        max_height=settings.frame_height,
        max_framerate=settings.framerate,
        # This process actually runs the device; Camera already defaults
        # to this for a normal construction, but it is spelled out here
        # because getting it wrong silently drops the hostname prefix on
        # its topic and disables its RPCs.
        synq_authoritive=True,
    )


def get_camera() -> Camera:
    """Return the process-wide camera, creating it on first use.

    One camera per process, not one per page: a camera is a device, and
    every instance declares its own SpiriSynq topic.

    Returns
    -------
    Camera
        The shared camera instance.
    """
    global _camera
    if _camera is None:
        settings = get_settings()
        _camera = _make_authoritive(settings.source or 'testimage://')
    return _camera


def _replace_camera(new_camera: Camera) -> Camera:
    """Install ``new_camera`` as the process-wide one, closing the old.

    Parameters
    ----------
    new_camera : Camera
        The camera to install.

    Returns
    -------
    Camera
        ``new_camera``, for chaining.
    """
    global _camera
    old, _camera = _camera, new_camera
    if old is not None:
        old.close()
    return new_camera


def set_authoritive(source_str: str = '') -> Camera:
    """Switch to running a device ourselves, replacing any mirror.

    Parameters
    ----------
    source_str : str
        Source to open.  Defaults to the test image when switching out
        of mirror mode, which never has a local source string of its
        own to fall back to.

    Returns
    -------
    Camera
        The new authoritative camera.
    """
    return _replace_camera(_make_authoritive(source_str or 'testimage://'))


def set_mirror(topic: str = '') -> Camera:
    """Switch to mirroring another node's camera, replacing any device.

    Parameters
    ----------
    topic : str
        Absolute SpiriSynq topic of the camera to mirror, as reported by
        :py:func:`discover_cameras`.  Empty to drop into mirror mode
        without yet picking one, e.g. to browse what is available.

    Returns
    -------
    Camera
        The new, non-authoritative camera.  A stopped device with
        nothing to show until a topic is given: its fields sit at their
        defaults rather than mirroring anything.
    """
    new_camera = Camera.from_topic(topic) if topic else Camera('', synq_authoritive=False)
    return _replace_camera(new_camera)


def discover_cameras(cam: Camera) -> list[dict]:
    """List Camera objects other nodes are currently advertising.

    Parameters
    ----------
    cam : Camera
        Any camera on the session to query from -- discovery is a
        property of the SpiriSynq session, not of a particular camera.

    Returns
    -------
    list[dict]
        One metadata dict per discovered camera, each with at least
        ``topic`` and ``authoritive_node``.  Only authoritative cameras
        ever answer this query, so a mirror never lists itself.
    """
    if not cam.synq_session:
        return []
    return list(cam.synq_session.list_topics(type_filter='Camera'))


def discover_widgets(cam: Camera) -> list[dict]:
    """List HudWidget objects other nodes are currently advertising.

    Parameters
    ----------
    cam : Camera
        Any camera on the session to query from; see
        :py:func:`discover_cameras`.

    Returns
    -------
    list[dict]
        One metadata dict per discovered widget, each with at least
        ``topic``.
    """
    if not cam.synq_session:
        return []
    return list(cam.synq_session.list_topics(type_filter='HudWidget'))


#: Sub-topic a widget created from this page publishes under, keyed by
#: name -- kept distinct from a camera's own topic and from
#: camera_metrics_widget's `f"{topic}_metrics"` so the three can never
#: collide. Not related to `Camera.overlay_widgets` (the per-camera field
#: naming which widgets to mirror) despite the similar name -- this is
#: only where a widget *this page creates* is published.
_UI_WIDGET_SUBTOPIC = 'ui_widgets'

#: Starting point for a freshly created widget -- small enough to read
#: at a glance, and a working example of `exif.*` templating rather than
#: an empty box. No `objects.*` example: that needs a real default topic
#: to declare (`{# object: alias = real/topic #}`, see overlay.py's
#: module docstring), which this generic starting point has no way to
#: guess.
_NEW_WIDGET_TEMPLATE = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="50">
  <rect width="200" height="50" fill="black" fill-opacity="0.5"/>
  <text x="8" y="30" font-family="{DEFAULT_FONT}" font-size="18" fill="white">Hello, {{{{ exif.topic }}}}</text>
</svg>'''

#: Embeds the same font file thorvg loads server-side as a browser
#: ``@font-face``, so `refresh_overlay`'s client-rendered SVG (widgets'
#: ``font-family="{DEFAULT_FONT}"``) resolves to Miracode in the browser
#: instead of silently falling back to a generic font -- which otherwise
#: makes overlay text look different between `overlay_client_render`
#: on and off even though every other pixel of placement matches.
#: A data URI rather than a static route: the font never changes at
#: runtime, and this avoids standing up static file serving just for it.
_DEFAULT_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{DEFAULT_FONT}'; "
    f"src: url(data:font/ttf;base64,{base64.b64encode(DEFAULT_FONT_PATH.read_bytes()).decode()}); }}"
)


def _overlay_topic_list(cam: Camera) -> list[str]:
    """Absolute topics of every widget ``cam`` currently mirrors."""
    return list(cam.overlay_widgets.keys())


def _overlay_add_topic(cam: Camera, topic: str) -> None:
    """Start mirroring the widget at ``topic``, if not already."""
    if topic not in cam.overlay_widgets:
        cam.overlay_widgets[topic] = {}


def _overlay_remove_topic(cam: Camera, topic: str) -> None:
    """Stop mirroring the widget at ``topic``, leaving the others."""
    cam.overlay_widgets.pop(topic, None)


#: Widgets this page created, keyed by absolute topic, so "remove" can
#: actually close them instead of merely detaching them from
#: overlay_widgets -- a widget discovered from elsewhere on the network
#: is never in here, and removing one of those only ever detaches it.
_ui_widgets: dict[str, HudWidget] = {}


def create_overlay_widget(cam: Camera, name: str) -> HudWidget:
    """Publish a new, editable HudWidget and add it to ``cam.overlay_widgets``.

    Parameters
    ----------
    cam : Camera
        The camera to attach the new widget to.
    name : str
        A short name for the widget, used to build its topic. Slugified
        the same way :py:func:`~SpiriCamera.camera.topic_for_source`
        slugifies a source string, so anything typeable works.

    Returns
    -------
    HudWidget
        The published widget, already added to ``cam.overlay_widgets``.
    """
    slug = re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_').lower() or 'widget'
    widget = HudWidget(
        synq_topic=f'{_UI_WIDGET_SUBTOPIC}/{slug}',
        synq_authoritive=True,
        svg_template=_NEW_WIDGET_TEMPLATE,
    )
    _ui_widgets[widget.synq_absolute_path] = widget
    _overlay_add_topic(cam, widget.synq_absolute_path)
    return widget


def add_metrics_widget(cam: Camera) -> HudWidget:
    """Publish a :py:func:`~SpiriCamera.overlay.camera_metrics_widget` for
    ``cam`` and add it to its own ``overlay_widgets``.

    Parameters
    ----------
    cam : Camera
        The camera to describe and attach the widget to.

    Returns
    -------
    HudWidget
        The published widget, already added to ``cam.overlay_widgets``.
    """
    widget = camera_metrics_widget(cam)
    _ui_widgets[widget.synq_absolute_path] = widget
    _overlay_add_topic(cam, widget.synq_absolute_path)
    return widget


def add_tiger_widget(cam: Camera) -> HudWidget:
    """Publish a :py:func:`~SpiriCamera.overlay.tiger_widget` for ``cam``
    and add it to its own ``overlay_widgets``.

    A worked example of percentage sizing (see
    :py:func:`~SpiriCamera.overlay.rasterize`) alongside the pixel-sized
    ``camera_metrics_widget`` example next to it.

    Parameters
    ----------
    cam : Camera
        The camera to attach the widget to.

    Returns
    -------
    HudWidget
        The published widget, already added to ``cam.overlay_widgets``.
    """
    widget = tiger_widget(cam)
    _ui_widgets[widget.synq_absolute_path] = widget
    _overlay_add_topic(cam, widget.synq_absolute_path)
    return widget


def _overlay_widget_usage(cam: Camera, topic: str) -> dict:
    """This camera's current per-widget override dict for ``topic``.

    A copy, not the live dict -- callers build on it and write the whole
    thing back via :py:func:`_overlay_set_position`/
    :py:func:`_overlay_set_binding`, since only ``EventedDict``'s own
    ``__setitem__`` is instrumented for sync; mutating a plain dict
    nested inside one of its values in place would not publish.
    """
    return dict(cam.overlay_widgets.get(topic, {}))


def _overlay_set_position(cam: Camera, topic: str, x: float | None, y: float | None) -> None:
    """Set (both given) or clear (either ``None``) this camera's position
    override for the widget at ``topic``, falling back to the widget's
    own anchor when cleared."""
    usage = _overlay_widget_usage(cam, topic)
    if x is None or y is None:
        usage.pop('x', None)
        usage.pop('y', None)
    else:
        usage['x'] = x
        usage['y'] = y
    cam.overlay_widgets[topic] = usage


def _overlay_set_binding(cam: Camera, topic: str, alias: str, object_topic: str) -> None:
    """Set (non-empty) or clear (empty) this camera's binding override
    for ``alias`` on the widget at ``topic``, falling back to that
    alias's own declared default when cleared."""
    usage = _overlay_widget_usage(cam, topic)
    bindings = dict(usage.get('bindings', {}))
    if object_topic:
        bindings[alias] = object_topic
    else:
        bindings.pop(alias, None)
    if bindings:
        usage['bindings'] = bindings
    else:
        usage.pop('bindings', None)
    cam.overlay_widgets[topic] = usage


def remove_overlay_widget(cam: Camera, topic: str) -> None:
    """Detach ``topic`` from ``cam.overlay_widgets``, closing it if this
    page is the one that created it.

    Parameters
    ----------
    cam : Camera
        The camera to detach the widget from.
    topic : str
        Absolute topic of the widget to remove, as it appears in
        ``cam.overlay_widgets``.
    """
    _overlay_remove_topic(cam, topic)
    widget = _ui_widgets.pop(topic, None)
    if widget is not None:
        widget.close()


def _frame_timestamp(frame: bytes) -> float:
    """The Unix timestamp ``frame`` was captured at, per its own EXIF.

    Read off the bytes in hand rather than from ``camera.exif_timestamp``,
    which by the time it is read may already describe a later frame.
    An untagged frame, or one whose timestamp is not a number, has no
    knowable capture time rather than a capture time of zero.

    Parameters
    ----------
    frame : bytes
        The encoded frame about to be served.

    Returns
    -------
    float
        Unix seconds, or ``0.0`` if the frame does not say.
    """
    try:
        return float(exif.extract(frame).get(exif.TIMESTAMP_TAG, ''))
    except ValueError:
        return 0.0


def _format_age(seconds: float) -> str:
    """Render a frame age at a resolution that suits its size.

    A stopped camera's frame keeps ageing, and milliseconds stop being
    a readable unit long before it is interesting again.

    Parameters
    ----------
    seconds : float
        The age to render.

    Returns
    -------
    str
        Something like ``26 ms old`` or ``4.2 s old``.
    """
    if seconds < 1:
        return f'{seconds * 1000:.0f} ms old'
    if seconds < 60:
        return f'{seconds:.1f} s old'
    return f'{seconds / 60:.0f} min old'


#: The frame object last counted by the meter, to recognise a re-serve.
#:
#: Compared by identity, not equality: the camera binds a fresh ``bytes``
#: to ``image`` for every frame it captures, so two captures that happen
#: to encode identically are still two frames, while the same object
#: handed out twice is one.
_last_counted: bytes | None = None

#: Guards the check-and-set on ``_last_counted``.
#:
#: Two clients polling at once can both land in ``serve_frame`` between
#: the read and the write, both see the old value, and both count the
#: same frame -- doubling the reported fps and KiB/s for as long as a
#: second tab stays open. The lock closes that window rather than just
#: documenting it.
_last_counted_lock = threading.Lock()


@app.get(FRAME_ROUTE)
def serve_frame() -> Response:
    """Serve the most recent frame as an image response.

    Returns
    -------
    Response
        The latest encoded frame, or a placeholder if none exists yet.
    """
    global _last_counted

    camera = get_camera()
    frame = camera.image
    if not frame:
        return PLACEHOLDER

    # Only new frames are metered. A stopped camera keeps its last frame
    # in `image`, and any browser still polling would otherwise have that
    # one frame counted over and over -- reporting a busy 30fps for a
    # camera that has produced nothing since it was stopped.
    with _last_counted_lock:
        is_new = frame is not _last_counted
        _last_counted = frame
    if is_new:
        frame_meter.record(len(frame), _frame_timestamp(frame))

    return Response(
        content=frame,
        media_type=camera.mimetype,
        # force_reload() already cache-busts with a timestamp; this stops
        # any proxy in between from holding on to a frame.
        headers={'Cache-Control': 'no-store'},
    )


def _format_tags(tags: dict[str, str]) -> str:
    """Render frame tags one per line, for the read-only display.

    Parameters
    ----------
    tags : dict[str, str]
        The tags read out of the current frame.

    Returns
    -------
    str
        One ``name: value`` per line, or a placeholder when untagged.
    """
    if not tags:
        return 'untagged'
    return '\n'.join(f'{name}: {value}' for name, value in sorted(tags.items()))


#: Provider name this page's own "Extra Tags" box tags under.
#:
#: A name of its own, rather than the default bucket exif_update() uses,
#: so a human poking at this debug field never clobbers tags some other
#: piece of software attached with exif_set_tags(); see
#: Camera.exif_set_tags for the general mechanism.
_UI_EXIF_PROVIDER = 'ui'


def _apply_extra_tags(camera: Camera, text: str) -> None:
    """Parse ``name=value`` pairs from a text field onto the camera.

    Ignores anything without an ``=``, so a half-typed entry does not
    throw away the tags already set.  Replaces only this page's own
    provider bucket -- see :py:data:`_UI_EXIF_PROVIDER` -- so it can
    never erase a tag another piece of software is managing.

    Parameters
    ----------
    camera : Camera
        The camera to tag.
    text : str
        Comma-separated ``name=value`` pairs.
    """
    tags = {}
    for pair in (text or '').split(','):
        name, separator, value = pair.partition('=')
        if separator and name.strip():
            tags[name.strip()] = value.strip()
    camera.exif_set_tags(_UI_EXIF_PROVIDER, tags)


def _source_options() -> dict[str, str]:
    """Map each known source string to a human label, for the picker.

    Returns
    -------
    dict[str, str]
        ``source_str -> label``, covering every bundled test pattern and
        every V4L2 device currently plugged in.
    """
    options = {
        f'testimage://{name}': f'Test pattern: {name}' for name in sorted(test_images())
    }
    for name in sorted(raster_test_images()):
        options[f'testimage://{name}'] = f'Test photo: {name}'
    for device in list_devices():
        options[device['path']] = device['label']
    return options


@ui.page('/')
def build_page():
    """Build the camera test UI page."""
    ui.add_head_html(f'<style>{_DEFAULT_FONT_FACE_CSS}</style>')

    def refresh_all() -> None:
        """Rebuild every camera-bound panel after the camera is swapped."""
        top_controls.refresh()
        settings_panels.refresh()
        overlay_panel.refresh()

    def toggle_authoritive(value: bool) -> None:
        """Rebuild the process-wide camera as a device, or as a mirror."""
        if value:
            set_authoritive(get_camera().source_str)
        else:
            set_mirror()
        refresh_all()

    def pick_source(source_str: str) -> None:
        """Retarget the current camera at a source picked from the list."""
        if source_str:
            get_camera().source_str = source_str

    def mirror(topic: str) -> None:
        """Start mirroring a camera, discovered or typed in by hand."""
        if not topic.strip():
            return
        set_mirror(topic.strip())
        refresh_all()

    @ui.refreshable
    def top_controls() -> None:
        """The device/mirror switch and whatever it puts above the frame.

        Split out from :py:func:`settings_panels` only so the frame
        viewer can sit between the two, matching where it always has --
        both panels are rebuilt together, by :py:func:`refresh_all`,
        whenever the camera is swapped out from under the page.
        """
        cam = get_camera()

        with ui.card().classes('w-full flex-shrink-0'):
            with ui.row().classes('w-full items-center'):
                ui.switch(
                    'Authoritative', value=cam.synq_authoritive,
                    on_change=lambda e: toggle_authoritive(e.value),
                )
                if cam.synq_authoritive:
                    ui.button('Start', on_click=cam.start).classes('bg-green-600 text-white')
                    ui.button('Stop', on_click=cam.stop).classes('bg-red-600 text-white')
                    # Debounced: every keystroke would otherwise retarget the camera.
                    ui.input('Source string').bind_value(cam, 'source_str').props(
                        'debounce=500'
                    ).classes('flex-1')
                    ui.select(
                        _source_options(), label='Known sources',
                        on_change=lambda e: pick_source(e.value),
                    ).classes('w-64')
                # Says "running", "stopped", or why it is neither.
                ui.label().bind_text_from(cam, 'status').classes('px-2 font-mono')

        ui.label().bind_text_from(cam.source, 'error').bind_visibility_from(
            cam.source, 'error'
        ).classes('w-full text-red-600 px-2')

        if not cam.synq_authoritive:
            # A mirror has no device of its own to start, and retargeting
            # its source_str would try to resolve it against *this*
            # machine's devices -- what mirroring means here is picking
            # someone else's camera, not editing this one's fields.
            with ui.card().classes('w-full flex-shrink-0'):
                with ui.row().classes('w-full items-center justify-between'):
                    ui.label('Cameras on the network').classes('text-lg font-bold')
                    ui.button(icon='refresh', on_click=top_controls.refresh).props('flat round')

                # Discovery is best-effort -- it relies on zenoh scouting
                # reaching the other node, which multicast-free networks,
                # routed links, and simple timing all defeat. Typing the
                # topic by hand is not a fallback for rare cases; it is
                # the one path that always works, so it is offered
                # alongside the list rather than hidden behind it.
                with ui.row().classes('w-full items-center gap-2'):
                    topic_input = ui.input(
                        'Topic', placeholder='hostname/spiricamera_testimage'
                    ).classes('flex-1')
                    ui.button('Mirror', on_click=lambda: mirror(topic_input.value))

                discovered = discover_cameras(cam)
                if not discovered:
                    ui.label(
                        'No other camera is currently advertising itself.'
                    ).classes('opacity-70')
                for meta in discovered:
                    topic = str(meta.get('topic', ''))
                    with ui.row().classes('w-full items-center gap-2'):
                        ui.label(topic).classes('font-mono flex-1')
                        ui.label(str(meta.get('authoritive_node', ''))).classes(
                            'opacity-70 text-xs'
                        )
                        ui.button('Mirror', on_click=lambda topic=topic: mirror(topic))

    @ui.refreshable
    def settings_panels() -> None:
        """The three detail cards below the frame, bound to the camera.

        Kept out of :py:func:`top_controls` only so the frame viewer can
        sit between the two; see that function's docstring.
        """
        cam = get_camera()

        with ui.row().classes('w-full gap-2 flex-shrink-0'):
            with ui.card().classes('flex-1'):
                ui.label('Image Settings').classes('text-lg font-bold')
                ui.label('Quality')
                with ui.row().classes('w-full items-center gap-2 flex-nowrap'):
                    ui.slider(min=1, max=100).bind_value(cam, 'quality', forward=int).classes('flex-1')
                    ui.number(min=1, max=100).bind_value(cam, 'quality', forward=int).classes('w-20')
                ui.number('Max Width').bind_value(cam, 'max_width', forward=int).classes('w-full')
                ui.number('Max Height').bind_value(cam, 'max_height', forward=int).classes('w-full')
                ui.number('Max Framerate').bind_value(cam, 'max_framerate', forward=int).classes('w-full')
                ui.input('Mimetype').bind_value(cam, 'mimetype').classes('w-full')
                ui.switch('Tag frames').bind_value(cam, 'exif_enabled')

            with ui.card().classes('flex-1'):
                ui.label('Device').classes('text-lg font-bold')
                ui.input('Vendor').bind_value(cam, 'vendor').classes('w-full')
                ui.input('Model').bind_value(cam, 'model').classes('w-full')
                ui.input('Serial Number').bind_value(cam, 'serial_number').classes('w-full')

                # Every field here is two-way on purpose. The source
                # overwrites the max_supported_* values each time it
                # opens, but nothing in SpiriSynq is truly read-only and
                # this is a debugging tool: being able to type a wrong
                # value in and watch what downstream does with it is the
                # point.
                ui.label('Capabilities').classes('text-lg font-bold pt-2')
                ui.number('Max Supported Width').bind_value(cam, 'max_supported_width').props('suffix="px"').classes('w-full')
                ui.number('Max Supported Height').bind_value(cam, 'max_supported_height').props('suffix="px"').classes('w-full')
                ui.number('Max Supported Framerate').bind_value(cam, 'max_supported_framerate').props('suffix="fps"').classes('w-full')

                # What actually arrived, as opposed to what was asked for
                # or advertised.
                ui.label('Received').classes('text-lg font-bold pt-2')
                ui.number('Received Width').bind_value(cam, 'received_width').props('suffix="px"').classes('w-full')
                ui.number('Received Height').bind_value(cam, 'received_height').props('suffix="px"').classes('w-full')
                ui.number('Received Ratio').bind_value(cam, 'received_ratio').props('step=0.0001').classes('w-full')
                ui.input('Synq Topic').bind_value(cam, 'synq_topic').classes('w-full')

            with ui.card().classes('flex-1'):
                ui.label('Resolved Source').classes('text-lg font-bold')
                ui.input('Status').bind_value(cam, 'status').classes('w-full')
                ui.input('Scheme').bind_value(cam.source, 'scheme').classes('w-full')
                ui.input('Target').bind_value(cam.source, 'target').classes('w-full')
                ui.input('Handler').bind_value(cam.source, 'handler').classes('w-full')
                ui.input('Error').bind_value(cam.source, 'error').classes('w-full')

                # Read out of the current frame's EXIF rather than synced
                # as fields of their own, so they cannot disagree with the
                # picture above. There is nowhere to write back to — the
                # frame already happened — which makes this the second
                # documented exception to the two-way binding rule, the
                # writable counterpart being Extra Tags below.
                ui.label('Frame Tags').classes('text-lg font-bold pt-2')
                ui.label().bind_text_from(
                    cam, 'exif_tags', backward=_format_tags
                ).classes('w-full font-mono text-xs whitespace-pre-wrap')

                ui.input(
                    'Extra Tags',
                    placeholder='mission=probe-1, operator=alex',
                    on_change=lambda event: _apply_extra_tags(cam, event.value),
                ).props('debounce=500').classes('w-full')

    @ui.refreshable
    def overlay_panel() -> None:
        """See, create, and edit the HudWidgets baked into this camera.

        ``@ui.refreshable`` tears down and rebuilds every element inside
        this function on ``.refresh()`` -- the right tool for a
        *structural* change (a widget added or removed changes how many
        rows exist), the wrong one for a value that only needs to update
        in place. Editable fields below use ``bind_value`` instead, which
        NiceGUI already keeps live on its own polling loop without ever
        recreating the element -- the same way Quality or Max Width bind
        two-way elsewhere on this page. ``.refresh()`` is therefore only
        called after an action that actually adds or removes a row
        (Create, Add, remove), plus the explicit refresh button below for
        "did an unresolved topic resolve yet" -- never on a timer, which
        previously blew away whatever you were typing (the new-widget
        name field, an in-progress SVG edit) every second.
        """
        cam = get_camera()

        with ui.card().classes('w-full flex-shrink-0'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Overlays').classes('text-lg font-bold')
                ui.button(icon='refresh', on_click=overlay_panel.refresh).props(
                    'flat round'
                )
            ui.switch('Render overlays in browser').bind_value(cam, 'overlay_client_render')
            with ui.row().classes('w-full items-center gap-2'):
                add_topic = ui.input(
                    'Add widget by topic',
                    placeholder='hostname/spiricamera_testimage_metrics',
                ).classes('flex-1')
                ui.button(
                    'Add',
                    on_click=lambda: (
                        _overlay_add_topic(cam, add_topic.value.strip()),
                        overlay_panel.refresh(),
                    ) if add_topic.value.strip() else None,
                )

            with ui.row().classes('w-full items-center gap-2'):
                new_widget_name = ui.input('New widget name').classes('flex-1')
                ui.button(
                    'Create & Add',
                    on_click=lambda: (
                        create_overlay_widget(cam, new_widget_name.value or 'widget'),
                        overlay_panel.refresh(),
                    ),
                )
                ui.button(
                    'Add CameraMetrics',
                    on_click=lambda: (add_metrics_widget(cam), overlay_panel.refresh()),
                )
                ui.button(
                    'Add Tiger',
                    on_click=lambda: (add_tiger_widget(cam), overlay_panel.refresh()),
                )

            active = _overlay_topic_list(cam)
            if not active:
                ui.label('No overlay topics configured.').classes('opacity-70')

            for topic in active:
                widget = cam._overlay_widgets.get(topic)
                with ui.card().classes('w-full').props('flat bordered'):
                    with ui.row().classes('w-full items-center gap-2'):
                        ui.label(topic).classes('font-mono flex-1 text-xs')
                        if widget is None:
                            ui.label('unresolved').classes('text-orange-600 text-xs')
                        ui.button(
                            icon='delete',
                            on_click=lambda topic=topic: (
                                remove_overlay_widget(cam, topic),
                                overlay_panel.refresh(),
                            ),
                        ).props('flat round color=red')

                    if widget is not None:
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.select(
                                list(ANCHORS), label='Anchor (default)',
                            ).bind_value(widget, 'anchor').classes('w-40')
                            ui.number('X % (default)').bind_value(
                                widget, 'custom_anchor_x'
                            ).bind_visibility_from(
                                widget, 'anchor', backward=lambda a: a == 'custom'
                            ).classes('w-24')
                            ui.number('Y % (default)').bind_value(
                                widget, 'custom_anchor_y'
                            ).bind_visibility_from(
                                widget, 'anchor', backward=lambda a: a == 'custom'
                            ).classes('w-24')

                        usage = cam.overlay_widgets.get(topic, {})
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Position override (this camera only):').classes(
                                'text-xs opacity-70'
                            )
                            override_x = ui.number('X %', value=usage.get('x')).classes('w-24')
                            override_y = ui.number('Y %', value=usage.get('y')).classes('w-24')
                            override_x.on_value_change(
                                lambda event, topic=topic, y=override_y: _overlay_set_position(
                                    cam, topic, event.value, y.value,
                                )
                            )
                            override_y.on_value_change(
                                lambda event, topic=topic, x=override_x: _overlay_set_position(
                                    cam, topic, x.value, event.value,
                                )
                            )
                            ui.button(
                                'Clear',
                                on_click=lambda topic=topic: (
                                    _overlay_set_position(cam, topic, None, None),
                                    overlay_panel.refresh(),
                                ),
                            ).props('flat dense')

                        for alias, default_topic in declared_objects(widget.svg_template).items():
                            bound_to = usage.get('bindings', {}).get(alias, '')
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label(f'{alias} (default: {default_topic})').classes(
                                    'font-mono text-xs flex-1'
                                )
                                ui.input(
                                    'Override object topic', value=bound_to,
                                ).props('debounce=500').classes('flex-1').on_value_change(
                                    lambda event, topic=topic, alias=alias: _overlay_set_binding(
                                        cam, topic, alias, event.value.strip(),
                                    )
                                )

                        ui.codemirror(language='XML', theme='basicDark').bind_value(
                            widget, 'svg_template'
                        ).classes('w-full')

            discovered = [
                meta.get('topic', '') for meta in discover_widgets(cam)
                if meta.get('topic', '') and meta.get('topic', '') not in active
            ]
            if discovered:
                ui.label('Discovered widgets').classes('text-md font-bold pt-2')
                for topic in discovered:
                    with ui.row().classes('w-full items-center gap-2'):
                        ui.label(topic).classes('font-mono flex-1 text-xs')
                        ui.button(
                            'Add',
                            on_click=lambda topic=topic: (
                                _overlay_add_topic(cam, topic),
                                overlay_panel.refresh(),
                            ),
                        )

    # Natural height, not h-screen: the frame is sized by width and the page
    # scrolls, rather than the frame being squeezed into whatever vertical
    # space the controls leave over.
    with ui.column().classes('w-full gap-2 p-2'):
        top_controls()

        # interactive_image's inner <img> is width:100%;height:100% with no
        # object-fit, so it stretches to whatever shape its box is. Letterbox
        # rather than warp, and let the wrapper's own aspect-ratio set the
        # height so the frame fills the available width instead of being
        # bounded by a short card.
        ui.add_css('.camera-frame img { object-fit: contain; }')

        # Plain CSS resize handle (bottom-right corner drag) rather than a
        # custom JS drag handler -- the browser already tracks the pointer
        # and clamps to min-width/min-height for us. interactive_image
        # normally derives its own height from the loaded image's aspect
        # ratio (nicegui/elements/interactive_image.js), which ignores
        # whatever height the card is dragged to; fill the card instead and
        # let object-fit letterbox any mismatch with the image's own ratio.
        ui.add_css('''
            .camera-viewport {
                resize: both;
                overflow: hidden;
                min-width: 160px;
                min-height: 90px;
                height: 360px;
            }
            .camera-wrap {
                position: relative;
                height: 100%;
            }
            .camera-frame {
                height: 100% !important;
            }
            .overlay-svg {
                position: absolute;
                inset: 0;
                width: 100%;
                height: 100%;
                pointer-events: none;
            }
        ''')

        # The overlay lives in its own element, laid over interactive_image
        # rather than injected into interactive_image's own <svg> (see
        # nicegui/elements/interactive_image.js): that <svg> is hardcoded
        # to preserveAspectRatio="none", stretching non-uniformly to fill
        # its box, so anything nested inside it inherits that same skew and
        # can't be un-distorted by giving the nested content its own
        # preserveAspectRatio. Two siblings sharing one box, each doing its
        # own "contain"/"meet" letterboxing against that same box, agree on
        # where the letterboxing falls without either needing to know about
        # the other.
        with ui.card().classes('camera-viewport w-full p-0'):
            with ui.element('div').classes('camera-wrap w-full'):
                frame = ui.interactive_image(FRAME_ROUTE).classes('camera-frame w-full')
                # sanitize=False: this is our own server-rendered overlay
                # SVG, not user-supplied HTML, and ui.html's default
                # sanitizer (the browser's native Sanitizer API) doesn't
                # reliably pass through SVG the way interactive_image's own
                # DOMPurify-with-svg-profile sanitizer did.
                overlay = ui.html('', sanitize=False).classes('overlay-svg')

        bandwidth = ui.label().classes('w-full px-2 font-mono text-sm opacity-70')

        def refresh_frame() -> None:
            """Pull the next frame, tracking the camera's current framerate."""
            timer.interval = 1 / max(1, int(get_camera().max_framerate or 1))
            frame.force_reload()
            refresh_overlay()

        def refresh_overlay() -> None:
            """Push live overlay SVG into the ``overlay`` element.

            Only does anything when ``overlay_client_render`` is set --
            otherwise the overlay, if any, is already burned into the
            frame itself server-side, and this clears any stale content
            left over from the switch having just been turned off.
            """
            cam = get_camera()
            body = ''
            if cam.overlay_client_render and cam.received_width and cam.received_height:
                body = cam.render_overlays_for_client(cam.received_width, cam.received_height)
                if body:
                    # Own top-level <svg>, sized against the same box as
                    # the image (see the .camera-frame CSS above) and
                    # letterboxed with "xMidYMid meet" against the frame's
                    # own resolution -- matching the image's object-fit:
                    # contain letterboxing without either element needing
                    # to know about the other.
                    body = (
                        f'<svg viewBox="0 0 {cam.received_width} {cam.received_height}" '
                        f'width="100%" height="100%" preserveAspectRatio="xMidYMid meet">'
                        f'{body}</svg>'
                    )
            overlay.set_content(body)

        def refresh_bandwidth() -> None:
            """Show the frame that arrived, and what the route is delivering.

            Three kinds of number share this line, and they part company
            when the camera stops.  The rates are rolling averages, so
            they fall to zero once no new frames are arriving — a stopped
            camera reads 0 fps even while a browser keeps polling and
            being handed the frame it already has.  The size and shape
            are an account of the last frame, which does not stop being
            true just because no frame followed it, so they stay put.
            The age is neither: the frame on screen really is getting
            older, so it keeps counting up.
            """
            cam = get_camera()
            parts = []
            if cam.received_width and cam.received_height:
                parts.append(f'{cam.received_width}x{cam.received_height}')
                parts.append(f'{cam.received_ratio:.3f}')

            # Age of the frame on screen, measured at the route where
            # frames actually leave. Against a remote camera it is only
            # as accurate as the two machines' clocks agree.
            age = frame_meter.age()
            if age:
                parts.append(_format_age(age))

            fps, bytes_per_second = frame_meter.rates()
            parts.append(f'{bytes_per_second / 1024:.0f} KiB/s')
            parts.append(f'{fps:.1f} fps')

            # Off the frame in hand, not off the rates: bytes-per-second
            # divided by frames-per-second is nothing at all once both are
            # zero, but the last frame is still exactly this big.
            if cam.image:
                parts.append(f'{len(cam.image) / 1024:.0f} KiB/frame')

            parts.append(f'requested {int(cam.max_framerate or 0)} fps')

            bandwidth.set_text(' · '.join(parts))

        timer = ui.timer(1 / max(1, int(get_camera().max_framerate or 1)), refresh_frame)
        # Filled in before the first tick, so the line reads 0 fps rather
        # than being blank for half a second on every page load.
        refresh_bandwidth()
        ui.timer(0.5, refresh_bandwidth)

        with ui.tabs().classes('w-full') as tabs:
            image_tab = ui.tab('Image')
            overlay_tab = ui.tab('Overlay')

        with ui.tab_panels(tabs, value=image_tab).classes('w-full'):
            with ui.tab_panel(image_tab).classes('w-full gap-2 p-0'):
                settings_panels()

            with ui.tab_panel(overlay_tab).classes('w-full gap-2 p-0'):
                overlay_panel()


if __name__ == '__main__':
    ui.run(title='SpiriCamera Test UI', reload=False, show=False, dark=None)


__all__ = [
    'FRAME_ROUTE',
    'add_metrics_widget',
    'add_tiger_widget',
    'build_page',
    'create_overlay_widget',
    'discover_cameras',
    'discover_widgets',
    'get_camera',
    'remove_overlay_widget',
    'serve_frame',
    'set_authoritive',
    'set_mirror',
]
