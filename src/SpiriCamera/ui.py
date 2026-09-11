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
import threading
import time
from collections import deque

from fastapi import Response
from nicegui import app, ui

from SpiriCamera import Camera, exif
from SpiriCamera.main import get_settings

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
        self._lock = threading.Lock()

    def record(self, size: int, age: float = 0.0) -> None:
        """Record one served frame.

        Parameters
        ----------
        size : int
            Size of the served frame in bytes.
        age : float
            Seconds between the frame being captured and being served.
            Zero for an untagged frame, which is not counted.
        """
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, size, age))
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
            Seconds, or ``0.0`` if no tagged frame has been served.
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            ages = [age for _, _, age in self._samples if age]
        if not ages:
            return 0.0
        return sum(ages) / len(ages)

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
        _camera = Camera(
            settings.source or 'testimage://',
            quality=settings.quality,
            max_width=settings.frame_width,
            max_height=settings.frame_height,
            max_framerate=settings.framerate,
        )
    return _camera


def _frame_age(frame: bytes) -> float:
    """Seconds since ``frame`` was captured, per its own EXIF.

    Read off the bytes in hand rather than from ``camera.exif_timestamp``,
    which by the time it is read may already describe a later frame.
    An untagged frame, or one whose timestamp is not a number, has no
    knowable age rather than an age of zero.

    Parameters
    ----------
    frame : bytes
        The encoded frame about to be served.

    Returns
    -------
    float
        Seconds since capture, or ``0.0`` if the frame does not say.
    """
    try:
        captured = float(exif.extract(frame).get(exif.TIMESTAMP_TAG, ''))
    except ValueError:
        return 0.0
    # Clamped: a remote camera whose clock is ahead of ours would
    # otherwise report a frame from the future.
    return max(0.0, time.time() - captured)


@app.get(FRAME_ROUTE)
def serve_frame() -> Response:
    """Serve the most recent frame as an image response.

    Returns
    -------
    Response
        The latest encoded frame, or a placeholder if none exists yet.
    """
    camera = get_camera()
    frame = camera.image
    if not frame:
        return PLACEHOLDER

    frame_meter.record(len(frame), _frame_age(frame))
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


def _apply_extra_tags(camera: Camera, text: str) -> None:
    """Parse ``name=value`` pairs from a text field onto the camera.

    Ignores anything without an ``=``, so a half-typed entry does not
    throw away the tags already set.

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
    camera.exif_extra = tags


@ui.page('/')
def build_page():
    """Build the camera test UI page."""
    cam = get_camera()

    # Natural height, not h-screen: the frame is sized by width and the page
    # scrolls, rather than the frame being squeezed into whatever vertical
    # space the controls leave over.
    with ui.column().classes('w-full gap-2 p-2'):
        with ui.card().classes('w-full flex-shrink-0'):
            with ui.row().classes('w-full items-center'):
                ui.button('Start', on_click=cam.start).classes('bg-green-600 text-white')
                ui.button('Stop', on_click=cam.stop).classes('bg-red-600 text-white')
                # Debounced: every keystroke would otherwise retarget the camera.
                ui.input('Source string').bind_value(cam, 'source_str').props(
                    'debounce=500'
                ).classes('flex-1')
                # Says "running", "stopped", or why it is neither.
                ui.label().bind_text_from(cam, 'status').classes('px-2 font-mono')

        ui.label().bind_text_from(cam.source, 'error').bind_visibility_from(
            cam.source, 'error'
        ).classes('w-full text-red-600 px-2')

        # interactive_image's inner <img> is width:100%;height:100% with no
        # object-fit, so it stretches to whatever shape its box is. Letterbox
        # rather than warp, and let the wrapper's own aspect-ratio set the
        # height so the frame fills the available width instead of being
        # bounded by a short card.
        ui.add_css('.camera-frame img { object-fit: contain; }')

        with ui.card().classes('w-full p-0 overflow-hidden'):
            frame = ui.interactive_image(FRAME_ROUTE).classes('camera-frame w-full')

        bandwidth = ui.label('idle').classes('w-full px-2 font-mono text-sm opacity-70')

        def refresh_frame() -> None:
            """Pull the next frame, tracking the camera's current framerate."""
            timer.interval = 1 / max(1, int(cam.max_framerate or 1))
            frame.force_reload()

        def refresh_bandwidth() -> None:
            """Show the frame that arrived, and what the route is delivering."""
            parts = []
            if cam.received_width and cam.received_height:
                parts.append(f'{cam.received_width}x{cam.received_height}')
                parts.append(f'{cam.received_ratio:.3f}')

            # Mean capture-to-serve age, taken at the route where frames
            # actually leave. Against a remote camera it is only as
            # accurate as the two machines' clocks agree.
            age = frame_meter.mean_age()
            if age:
                parts.append(f'{age * 1000:.0f} ms old')

            fps, bytes_per_second = frame_meter.rates()
            if fps:
                parts.append(f'{bytes_per_second / 1024:.0f} KiB/s')
                parts.append(f'{fps:.1f} fps')
                parts.append(f'{bytes_per_second / fps / 1024:.0f} KiB/frame')
                parts.append(f'requested {int(cam.max_framerate or 0)} fps')
            else:
                parts.append('idle')

            bandwidth.set_text(' · '.join(parts))

        timer = ui.timer(1 / max(1, int(cam.max_framerate or 1)), refresh_frame)
        ui.timer(0.5, refresh_bandwidth)

        with ui.row().classes('w-full gap-2 flex-shrink-0'):
            with ui.card().classes('flex-1'):
                ui.label('Image Settings').classes('text-lg font-bold')
                ui.label('Quality')
                with ui.row().classes('w-full items-center gap-2 flex-nowrap'):
                    ui.slider(min=1, max=100).bind_value(cam, 'quality').classes('flex-1')
                    ui.number(min=1, max=100).bind_value(cam, 'quality').classes('w-20')
                ui.number('Max Width').bind_value(cam, 'max_width').classes('w-full')
                ui.number('Max Height').bind_value(cam, 'max_height').classes('w-full')
                ui.number('Max Framerate').bind_value(cam, 'max_framerate').classes('w-full')
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


if __name__ == '__main__':
    ui.run(title='SpiriCamera Test UI', reload=False, show=False, dark=None)


__all__ = ['FRAME_ROUTE', 'build_page', 'get_camera', 'serve_frame']
