# Architecture

This page is for anyone changing SpiriCamera itself, rather than just
using a `Camera`. It covers the things that only live in scattered
docstrings today: how the package is split into domains, how a
`Camera` behaves as a SpiriSynq object, how to add a new source
handler, and what the debug UI's bandwidth/latency readout actually
measures.

## Domain split: Camera vs. sources

The package has one dependency direction, enforced by convention, not
by tooling:

```text
SpiriCamera.camera  ->  SpiriCamera.sources  ->  SpiriCamera.sources.base
```

- **`SpiriCamera.sources`** (and its handler modules—`v4l`, `network`,
  `file`, `testimage`, `spirisynq`) owns *how to reach a device*: parsing a source
  string, opening it, and handing back raw BGR `numpy` arrays. A
  handler never imports {py:class}`~SpiriCamera.camera.Camera` and
  knows nothing about JPEG encoding, SpiriSynq, or EXIF.
- **`SpiriCamera.camera`** owns *what happens to a frame once you have
  one*: the capture thread, encoding to JPEG, tagging, and publishing
  via `SyncableObject`. It talks to a source only through the
  {py:class}`~SpiriCamera.sources.base.SourceBase` contract.
- **`SpiriCamera.transports`** owns the wire: the transport objects, what a
  frame looks like on zenoh, and who wants it. `sources/spirisynq.py` needs
  these names too, so it never imports `camera.py` either.
- **`SpiriCamera.video`** owns the one H.264 encode and its rate control.
- **`SpiriCamera.rtsp`** serves that encode to anything outside SpiriSynq,
  with KLV metadata from **`SpiriCamera.klv`**.
- **`SpiriCamera.exif`** is used by `camera.py` to embed/extract tags,
  and by `ui.py` to read a frame's capture timestamp back out. It does
  not know about `Camera` or sources either.

If you find yourself importing `SpiriCamera.camera` from inside
`SpiriCamera/sources/`, that's the layering breaking—a source should
never need to know it's being driven by a `Camera`.

## Camera as a SpiriSynq object

`Camera` is a `SyncableObject` ({py:class}`~SpiriCamera.camera.CameraBase`
declares the synced fields; `Camera` adds behaviour on top). It is a
SpiriSynq `Position` in particular: `relative_to` and `mount` say what
it is mounted on and how, and `pose` is where it is and which way it
looks, which is what lets an overlay draw on things in the world
({ref}`world-overlays`). Two things about it are easy to get wrong:

**Nothing is read-only.** Fields like `max_supported_width` or
`received_width` are described as "what the device reported" or "what
was actually received," but they are ordinary writable fields—a
remote peer can already set any of them over SpiriSynq, so a property
setter or a one-way binding would only hide that, not enforce it. This
is why `ui.py` binds every field two-way with `bind_value` rather than
`bind_value_from`: being able to type a wrong value in and watch what
downstream does with it is a deliberate debugging feature, not an
oversight. The only fields with a one-way binding in the debug UI are
ones with no meaningful counterpart to write back to—the bandwidth
meter and the read-out frame tags (see below).

**Authoritative vs. mirror.** A `Camera` you construct normally
(`Camera("/dev/video0")`) is *authoritative*: it actually owns the
device, and SpiriSynq prefixes its topic with this machine's hostname
and exposes its `@remote_method` RPCs. A `Camera` rebuilt from a
SpiriSynq rehydrate reply (`Camera.from_topic(...)` on a remote peer)
is a *mirror*: same synced fields, but it never resolves `source_str`
into a local handler, because a device path on the mirror's machine
may mean something completely different (or nothing at all) locally.
A mirror therefore always starts with `handler is None` and cannot
`start()`—it exists to observe, not to capture. This is controlled
by the `synq_authoritive` constructor argument, which defaults to the
right answer for both cases; pass it explicitly only if you're doing
something unusual (see `ui.py`'s `get_camera()` for an example of
spelling it out for clarity even though the default already does the
right thing).

**Frames are not fields.** A transport's settings are ordinary synced
fields, but its frames travel on `@` keys that no wildcard matches, and
`data` is a property with its own `data_changed` signal rather than a
dataclass field. A field would be relayed through the camera's whole
event tree once per frame. It also escaped into SpiriSynq's YAML
publishing once, so don't turn it back into one. {doc}`data_model` has the
whole structure.

**The camera runs on demand.** `running` is set by the capture loop from
its subscribers, and any peer's write to it is overwritten on the next
pass. `DataChannel.demanded` is the signal: the key's publisher matching a
subscriber, or a `hold` from something in this process. `start()` opens
the source once to publish what it is, then closes it until something
wants frames. `start()` and `stop()` are local intent, so nothing remote
can switch a camera off.

**A live source is never left to queue.** A network stream or a V4L2
device sends frames whether or not they're read, so frames read late wait
in FFmpeg's, the kernel's or the driver's buffers, and that wait is
latency: a capture loop that paced its reads to `max_framerate` fell
behind a 30 fps RTSP camera by the whole of a UDP receive buffer, about
three seconds. So a source with `live` set reads its device on a thread
of its own, as fast as it delivers, and `read()` waits for and hands back
only the newest frame, dropping any that come sooner than `max_framerate`
allows. The capture loop doesn't pace it as well. Files, test patterns and
`spirisynq://` relays hand over a frame when asked, so the loop paces them.

**Frame-derived fields never travel as their own field.** `received_width`,
`received_height`, `received_ratio`, and the EXIF-derived
`exif_tags`/`exif_timestamp` all describe one specific frame. SpiriSynq
syncs each field independently with no ordering guarantee between two
fields, so publishing "this frame is 1920x1080" next to "here is the
image" is a race—a peer can read the width against a *different*
image than the one it names. Instead these values travel with the
frame, in a JPEG's EXIF ({py:mod}`SpiriCamera.exif`) or the attachment of
a raw frame or video packet, and are read back out by
`Camera._observe_frame`, identically on the authoritative node and on
every mirror. If you add a new field that
describes the current frame rather than the camera's own settings, it
almost certainly belongs in the frame's tags, not as a new synced attribute—copy
the pattern of `received_width` rather than adding a sibling to
`max_width`.

## Adding a new source handler

A handler is a `SourceBase` subclass that gets discovered automatically
by being imported—there's no registry to edit. Concretely, to add
one:

1. Create `src/SpiriCamera/sources/my_protocol.py`.
2. Subclass {py:class}`~SpiriCamera.sources.base.SourceBase` (or
   {py:class}`~SpiriCamera.sources.capture.OpenCVSource` if
   `cv2.VideoCapture` can already speak your protocol—this is what
   `v4l.py` and `network.py` do; only write against the raw
   `SourceBase` contract if you need something OpenCV can't do, the
   way `testimage.py` renders SVGs itself).
3. Set `schemes = ("myproto",)` and implement `open`, `close`, `read`,
   and `is_open`. Override `handles()` too if you need to claim
   schemeless strings the way `v4l.py` claims bare device paths.
4. Import the module from `src/SpiriCamera/sources/__init__.py`'s
   `# Importing these modules is what registers their handlers` block.

Minimal skeleton, modeled on the real handlers:

```python
"""My protocol camera source."""

from __future__ import annotations

import numpy as np

from SpiriCamera.sources.base import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceURL,
)


class MyProtocolSource(SourceBase):
    """Reads frames from my-protocol://<target>."""

    schemes = ("myproto",)

    def __init__(self, url: SourceURL) -> None:
        super().__init__(url)
        self._device = None

    @property
    def is_open(self) -> bool:
        return self._device is not None

    def open(self, settings: CaptureSettings) -> SourceCapabilities:
        try:
            self._device = _connect(self.target, settings)
        except OSError as exc:
            raise SourceError(f"could not open {self.target!r}: {exc}") from exc
        return SourceCapabilities(
            max_supported_width=self._device.width,
            max_supported_height=self._device.height,
        )

    def close(self) -> None:
        if self._device is not None:
            self._device.release()
            self._device = None

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        # Honour `settings` on every call, not just at open() -- this is
        # what lets a live resolution/framerate change take effect
        # without restarting the camera.
        return self._device.grab_frame(settings)
```

A few contract details that are easy to miss because they only show up
as bugs in a UI far from the handler:

- **`read()` may return `None`** when there's no new frame yet (for example a
  network stream that hasn't delivered one). The camera then
  keeps the previous frame rather than raising, so returning
  `None` is the normal way to say nothing changed, and is not an error.
- **Return the *same* array object** for an unchanged frame (a static
  test pattern, a paused file) rather than an equal-but-distinct copy.
  `Camera._encode` caches by identity (`is`, not `==`) specifically so
  a source that isn't producing new pixels doesn't pay for re-encoding
  every capture tick.
- **`open()` on an already-open source must be a no-op** that just
  re-reports capabilities—`Camera.start()` on an already-running
  camera doesn't call `open()` at all, but a source can still see a
  redundant `open()` from other paths and must not double-acquire the
  device.
- **Never raise from resolution** (`handles`/`from_url` aside from a
  genuinely malformed target)—an unresolvable or not-yet-openable
  source should surface through `SourceError` at `open()`/`read()`
  time, because `Camera` is designed to be constructed from a
  half-typed, live-edited source string without raising. If your
  handler needs validation, do it in `open()`, not in the constructor.

(the-overlay-layer)=
## The overlay layer

{py:mod}`SpiriCamera.overlay` bakes SVG "HUD" widgets into a frame after it
is resized and before it becomes `transport_raw.data`, so every transport
carries them. Frames are read-only, so the first widget copies the frame
to draw on it, and a frame with no widgets is never copied. A widget ({py:class}`~SpiriCamera.overlay.HudWidget`) is
its own `SyncableObject`, published wherever its author runs (a UI
panel, an ML detector)—a camera never creates one, it only mirrors
the widgets named in its `overlay_widgets` field
({py:class}`~SpiriCamera.overlay.OverlayMixin`, mixed into
{py:class}`~SpiriCamera.camera.CameraBase`). This split—author owns
the widget, camera only opts in—is what lets several cameras share
one widget definition while each binds it to its own data and
placement.

A widget's SVG is templated with MiniJinja against three separate
namespaces (plus `view` and `item` for a widget placed on the world;
see below), and *why there are three* is the one thing worth
understanding before touching this code:

- `exif`: the current frame's tags.
- `frame`: the current frame's actual, as-received width/height/
  framerate and a render-time timestamp. These are injected directly
  rather than read through `objects` because they are exactly the
  fields the "Frame-derived fields never travel as their own field"
  rule above forbids publishing as synced attributes—there is no
  topic a widget could declare to reach them.
- `objects`: the latest field values of any other SpiriSynq object a
  widget declares for itself, by alias, via a
  `{# object: <alias> = <default topic> #}` comment next to where the
  alias is used. The alias exists because a real topic can contain
  characters (a hyphen in a hostname) that are not valid inside a
  `{{ }}` expression, and because a camera rendering the widget can
  rebind an alias to a different concrete object without touching the
  shared widget definition (see `overlay_widgets`' `"bindings"` key).

`SpiriCamera.overlay.resolve_objects` cross-checks MiniJinja's own
static analysis of what a template *reads* against what it
*declares*, and raises immediately if an alias is used with no
matching declaration—a widget author gets that error at authoring
time rather than a blank value baked into every frame.

**Rendering never scales.** `rasterize()` renders each widget at its
own natural pixel size (`Picture.get_size()`); `composite()` alpha
blends it onto the frame at whatever position `anchor_position()`
computes and clips the rest. A widget author who writes
`font-size="24"` gets a real 24px regardless of the camera's
resolution—scaling the raster to fit a declared box was rejected
because it either blurs small text or makes it illegible on a 4K
frame. Text is drawn by `thorvg`, using the bundled `Miracode` font
registered once at import time; `thorvg` never falls back to an
OS-installed font, so a widget that references an unloaded font family
renders nothing for that text.

**Failure is per-widget, per-frame, and never fatal.** A widget that
fails to render—a template error, a declared object with no value
yet—is skipped for that frame and logged once
(`OverlayMixin._overlay_complain` de-duplicates by message so a
widget that keeps failing does not spam the log every frame). One
broken overlay is not allowed to stop the camera from publishing.

**Mirroring is lazy and cheap to re-check.** `_overlay_sync_widgets`
and `_overlay_sync_objects` run once per frame but only do network work
(mirroring a widget or a data-source object) the first time a topic is
seen; anything already mirrored is trusted to update itself live via
its own SpiriSynq subscription. That is what makes it safe to call
every frame, which is how new or rebound
`overlay_widgets` entries get picked up without wiring
fine-grained `EventedDict` change signals.

**Placing a widget on the world is opt-in, and the template never does
the geometry.** A widget that declares `{# anchor: ... #}` or
`{# project: ... #}` (`world_placement()`) is drawn by where its objects
are in the frame tree: `_overlay_renders()` projects their poses through
the camera's own pose and lens ({py:mod}`SpiriCamera.projection`), and
either centres a copy of the widget on each, or hands a frame-wide
widget the pixels as `view`. That is why a camera is a `Position`. A
relay camera follows its upstream's pose and lens through the source
contract (`SourceCapabilities.frame`, `SourceBase.frame_intrinsics()`), so
a source never reaches back into the camera. Both render paths, burned
in and client-side, go through `_overlay_renders()` and
`_overlay_position()`, so they place widgets the same way. The guide for
widget authors is {ref}`world-overlays`.

See the {py:mod}`SpiriCamera.overlay` module docstring for the full
reasoning, and {py:func}`~SpiriCamera.overlay.camera_metrics_widget`
for a worked example of a ready-made widget that reads both `frame`
and its own camera's live fields via `objects`.

## The debug UI's bandwidth and latency readout

`SpiriCamera.ui` runs one `Camera` per process (`get_camera()`, module
global) and serves its JPEG frames over a plain HTTP route
(`GET /camera/frame`). Polling that route is what subscribes it to
`transport_jpeg`, and five seconds without a poll unsubscribes it, so
closing the tab lets the camera idle. It serves them over HTTP rather than pushing them over the websocket as
base64—`ui.image`/`QImg` cross-fades on every source change, which at
video framerate looks like a permanent dim pulse, and pushing to every
client regardless of whether it can keep up defeats the point of a
pull-based readout. Read the module docstring in
{py:mod}`SpiriCamera.ui` for the full reasoning; the parts worth
knowing if you're debugging the numbers it shows:

- **fps / KiB/s / kilobits per second** come from `RateMeter`, which is fed only when
  `serve_frame()` sees a *new* frame object (compared by identity, via
  `_last_counted`)—a stopped camera under repeated polling reports
  0 fps rather than the rate of the polling. `_last_counted_lock`
  exists because two browser tabs can both read the stale value before
  either writes the new one, which without the lock double-counts a
  single frame across two concurrent requests.
- **Frame age** is *not* "now minus this frame's timestamp" sampled
  once—that number saws between zero and a full frame interval
  depending on when exactly you read it, and says more about polling
  phase than actual latency. `RateMeter.age()` instead reports
  `max(mean_age_over_window, true_age_of_newest_frame_seen)`: the mean
  is the stable number while frames are flowing, and the true age is
  what it decays into once they stop—a camera that died five minutes
  ago should read "5 min old" and not fall back to a rolling average of
  zero.
- **Clock skew**: age is computed from the frame's own EXIF capture
  timestamp against `time.time()` on the machine serving the debug UI,
  clamped at zero. Against a remote/mirrored camera this is only as
  good as the agreement between the two machines' clocks—an age of
  "0 ms" on two clocks set far apart does not mean zero latency, it means the
  remote clock is ahead.

## RTSP egress

{py:mod}`SpiriCamera.rtsp` is how a camera leaves SpiriSynq for anything else,
and {ref}`rtsp` covers using it. The server runs in the camera's own process, on
a thread and event loop of its own, and reads `transport_video.packet_received`
as any other in-process consumer does. A session holds the transport while it
plays, which is what keeps the camera encoding, so RTSP viewers and zenoh
subscribers all share one encode ({py:class}`~SpiriCamera.video.VideoEncoder`):
libx264, at `SPIRICAMERA_H264_PRESET` (default `veryfast`), on its own thread so
a slow x264 holds up neither capture nor JPEG.

It's written by hand rather than with a library, so that it can act on RTCP
however rate control needs. {py:class}`~SpiriCamera.rtsp.LossMeter` holds that
logic, and {py:class}`~SpiriCamera.video.RateController` knows nothing of RTSP.

Two things GStreamer does shaped it:

- **Reports are slow, and empty during loss.** On a clean link a receiver report
  comes every 3 to 7.5 seconds, past the default `stall_timeout`, so a session sends
  rate control a keepalive each second. While it's sending NACKs, the RTCP around
  them has no report blocks, so loss has to come from the NACKs themselves.
- **A shared CNAME stops decoding.** Giving both tracks one CNAME asks `rtpbin` to
  synchronize them, and QGroundControl then receives frames and never decodes one.
  Each track has its own, so RTP timestamps alone tie a KLV packet to its frame.

## Sources that relay, and sources that come and go

**Another camera is a source too.** `SpiriCamera.sources.spirisynq.SpiriSynqSource`
(`spirisynq://<topic>`, also `spirisynq+jpeg://`, `spirisynq+raw://` and
`spirisynq+video://`) mirrors an upstream camera on `open()` for its
fields, and takes frames straight off the transport's `@data` key, decoded
and fitted to the requested bounds. It subscribes from its first `read()`
until `close()`, so only while its own camera is running, which is how
demand travels up a chain of relays. It never imports `camera.py`: it looks the upstream's type tag up with
`Session.list_topics` and mirrors it with whatever class the session has
registered under it (the real `Camera`, in any process running one),
falling back to `from_topic_untyped` only when there is none. The order
matters—`from_topic_untyped` re-registers the tag with a synthesized
class, which breaks every later `Camera.from_topic` on that session.
`Camera.start()` opens its handler under `synq_session.as_default()`,
so the mirror lands on the camera's own session. Upstream frame
tags come through `SourceBase.frame_tags()`, merged under the camera's
own.

**Sources that are not there yet, or any more.** A wanted background camera
treats a `SourceError` from `open()` as "not yet": it stays `running`,
reports `waiting for source: <reason>`, and the capture loop retries
the open every `_SOURCE_RETRY_INTERVAL`. The loop reopens any handler
whose `is_open` goes false, so the contract for a source that loses
its device mid-stream is simply to close itself and raise from
`read()`. `V4LSource` does that when a failed read finds its device
node gone (a failed read with the node present is only a dropped
frame). `SpiriSynqSource` does it on the upstream's tombstone (a clean
shutdown), or—since a crash sends none—when no frame has arrived
for `STALE_AFTER` seconds and the topic no longer answers
`list_topics`, or answers from a different process. It uses
`list_topics` rather than an `sr_metadata` RPC for this because an
absent topic is just no reply there, where the RPC logs an error
on every retry.

## Where things are tested

`tests/` exercises the source handlers and `Camera` against real
`SpiriSynq` peers over a namespaced zenoh session (see recent history
around "namespace the test suite's zenoh peer" for why—tests must not
collide with a zenoh session already running on the same machine). Run
the full suite with:

```console
python -m pytest
python -m pytest --cov=SpiriCamera
```

When adding a new source handler, add a corresponding test module
alongside the existing ones rather than folding it into
`test_camera.py`—handler tests exercise `open`/`close`/`read` in
isolation from the capture thread, which is what makes it possible to
test protocol edge cases without racing a background thread.
