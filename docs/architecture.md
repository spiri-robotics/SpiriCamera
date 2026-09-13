# Architecture

This page is for anyone changing SpiriCamera itself, rather than just
using a `Camera`. It covers the things that only live in scattered
docstrings today: how the package is split into domains, how a
`Camera` behaves as a SpiriSynq object, how to add a new source
handler, and what the debug UI's bandwidth/latency readout actually
measures.

## Domain split: camera vs. sources

The package has one dependency direction, enforced by convention, not
by tooling:

```text
SpiriCamera.camera  ->  SpiriCamera.sources  ->  SpiriCamera.sources.base
```

- **`SpiriCamera.sources`** (and its handler modules — `v4l`, `network`,
  `file`, `testimage`) owns *how to reach a device*: parsing a source
  string, opening it, and handing back raw BGR `numpy` arrays. A
  handler never imports {py:class}`~SpiriCamera.camera.Camera` and
  knows nothing about JPEG encoding, SpiriSynq, or EXIF.
- **`SpiriCamera.camera`** owns *what happens to a frame once you have
  one*: the capture thread, encoding to JPEG, tagging, and publishing
  via `SyncableObject`. It talks to a source only through the
  {py:class}`~SpiriCamera.sources.base.SourceBase` contract.
- **`SpiriCamera.exif`** is used by `camera.py` to embed/extract tags,
  and by `ui.py` to read a frame's capture timestamp back out. It does
  not know about `Camera` or sources either.

If you find yourself importing `SpiriCamera.camera` from inside
`SpiriCamera/sources/`, that's the layering breaking — a source should
never need to know it's being driven by a `Camera`.

## Camera as a SpiriSynq object

`Camera` is a `SyncableObject` ({py:class}`~SpiriCamera.camera.CameraBase`
declares the synced fields; `Camera` adds behaviour on top). Two things
about that are easy to get wrong:

**Nothing is read-only.** Fields like `max_supported_width` or
`received_width` are described as "what the device reported" or "what
was actually received", but they are ordinary writable fields — a
remote peer can already set any of them over SpiriSynq, so a property
setter or a one-way binding would only hide that, not enforce it. This
is why `ui.py` binds every field two-way with `bind_value` rather than
`bind_value_from`: being able to type a wrong value in and watch what
downstream does with it is a deliberate debugging feature, not an
oversight. The only fields with a one-way binding in the debug UI are
ones with no meaningful counterpart to write back to — the bandwidth
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
`start()` — it exists to observe, not to capture. This is controlled
by the `synq_authoritive` constructor argument, which defaults to the
right answer for both cases; pass it explicitly only if you're doing
something unusual (see `ui.py`'s `get_camera()` for an example of
spelling it out for clarity even though the default already does the
right thing).

**Frame-derived fields never travel as their own field.** `received_width`,
`received_height`, `received_ratio`, and the EXIF-derived
`exif_tags`/`exif_timestamp` all describe one specific frame. SpiriSynq
syncs each field independently with no ordering guarantee between two
fields, so publishing "this frame is 1920x1080" next to "here is the
image" is a race — a peer can read the width against a *different*
image than the one it names. Instead these values are written into the
JPEG's EXIF by {py:mod}`SpiriCamera.exif` and read back out of
`cam.image` by `Camera._on_image_changed`, identically on the
authoritative node and on every mirror. If you add a new field that
describes "the current frame" rather than "the camera's settings", it
almost certainly belongs in EXIF, not as a new synced attribute — copy
the pattern of `received_width` rather than adding a sibling to
`max_width`.

## Adding a new source handler

A handler is a `SourceBase` subclass that gets discovered automatically
by being imported — there's no registry to edit. Concretely, to add
one:

1. Create `src/SpiriCamera/sources/my_protocol.py`.
2. Subclass {py:class}`~SpiriCamera.sources.base.SourceBase` (or
   {py:class}`~SpiriCamera.sources.capture.OpenCVSource` if
   `cv2.VideoCapture` can already speak your protocol — this is what
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

- **`read()` may return `None`** when there's no new frame yet (e.g. a
  network stream that hasn't delivered one). `Camera.read()` then
  re-serves the previous `cam.image` rather than raising, so returning
  `None` is the normal way to say "nothing changed", not an error.
- **Return the *same* array object** for an unchanged frame (a static
  test pattern, a paused file) rather than an equal-but-distinct copy.
  `Camera._encode` caches by identity (`is`, not `==`) specifically so
  a source that isn't producing new pixels doesn't pay for re-encoding
  every capture tick.
- **`open()` on an already-open source must be a no-op** that just
  re-reports capabilities — `Camera.start()` on an already-running
  camera doesn't call `open()` at all, but a source can still see a
  redundant `open()` from other paths and must not double-acquire the
  device.
- **Never raise from resolution** (`handles`/`from_url` aside from a
  genuinely malformed target) — an unresolvable or not-yet-openable
  source should surface through `SourceError` at `open()`/`read()`
  time, because `Camera` is designed to be constructed from a
  half-typed, live-edited source string without raising. If your
  handler needs validation, do it in `open()`, not in the constructor.

(the-overlay-layer)=
## The overlay layer

{py:mod}`SpiriCamera.overlay` bakes SVG "HUD" widgets into a frame before
it is encoded. A widget ({py:class}`~SpiriCamera.overlay.HudWidget`) is
its own `SyncableObject`, published wherever its author runs (a UI
panel, an ML detector) — a camera never creates one, it only mirrors
the widgets named in its `overlay_widgets` field
({py:class}`~SpiriCamera.overlay.OverlayMixin`, mixed into
{py:class}`~SpiriCamera.camera.CameraBase`). This split — author owns
the widget, camera only opts in — is what lets several cameras share
one widget definition while each binds it to its own data and
placement.

A widget's SVG is templated with MiniJinja against three separate
namespaces, and *why there are three* is the one thing worth
understanding before touching this code:

- `exif` — the current frame's tags.
- `frame` — the current frame's actual, as-received width/height/
  framerate and a render-time timestamp. These are injected directly
  rather than read through `objects` because they are exactly the
  fields the "Frame-derived fields never travel as their own field"
  rule above forbids publishing as synced attributes — there is no
  topic a widget could declare to reach them.
- `objects` — the latest field values of any other SpiriSynq object a
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
matching declaration — a widget author gets that error at authoring
time rather than a silently blank value baked into every frame.

**Rendering never scales.** `rasterize()` renders each widget at its
own natural pixel size (`Picture.get_size()`); `composite()` alpha
blends it onto the frame at whatever position `anchor_position()`
computes and clips the rest. A widget author who writes
`font-size="24"` gets a real 24px regardless of the camera's
resolution — scaling the raster to fit a declared box was rejected
because it either blurs small text or makes it illegible on a 4K
frame. Text is drawn by `thorvg`, using the bundled `Miracode` font
registered once at import time; `thorvg` never falls back to an
OS-installed font, so a widget that references an unloaded font family
renders nothing for that text, silently.

**Failure is per-widget, per-frame, and never fatal.** A widget that
fails to render — a template error, a declared object with no value
yet — is skipped for that frame and logged once
(`OverlayMixin._overlay_complain` de-duplicates by message so a
persistently-broken widget does not spam the log every frame). One
broken overlay is not allowed to stop the camera from publishing.

**Mirroring is lazy and cheap to re-check.** `_overlay_sync_widgets`
and `_overlay_sync_objects` run once per frame but only do network work
(mirroring a widget or a data-source object) the first time a topic is
seen; anything already mirrored is trusted to update itself live via
its own SpiriSynq subscription. That is what makes it safe to call
every frame, which is how newly-added or newly-rebound
`overlay_widgets` entries get picked up promptly without wiring
fine-grained `EventedDict` change signals.

See the {py:mod}`SpiriCamera.overlay` module docstring for the full
reasoning, and {py:func}`~SpiriCamera.overlay.camera_metrics_widget`
for a worked example of a ready-made widget that reads both `frame`
and its own camera's live fields via `objects`.

## The debug UI's bandwidth and latency readout

`SpiriCamera.ui` runs one `Camera` per process (`get_camera()`, module
global) and serves its frames over a plain HTTP route
(`GET /camera/frame`) rather than pushing them over the websocket as
base64 — `ui.image`/`QImg` cross-fades on every source change, which at
video framerate looks like a permanent dim pulse, and pushing to every
client regardless of whether it can keep up defeats the point of a
pull-based readout. Read the module docstring in
{py:mod}`SpiriCamera.ui` for the full reasoning; the parts worth
knowing if you're debugging the numbers it shows:

- **fps / KiB/s** come from `RateMeter`, which is fed only when
  `serve_frame()` sees a *new* frame object (compared by identity, via
  `_last_counted`) — a stopped camera being polled repeatedly reports
  0 fps rather than the rate of the polling. `_last_counted_lock`
  exists because two browser tabs can both read the stale value before
  either writes the new one, which without the lock double-counts a
  single frame across two concurrent requests.
- **Frame age** is *not* "now minus this frame's timestamp" sampled
  once — that number saws between zero and a full frame interval
  depending on when exactly you read it, and says more about polling
  phase than actual latency. `RateMeter.age()` instead reports
  `max(mean_age_over_window, true_age_of_newest_frame_seen)`: the mean
  is the stable number while frames are flowing, and the true age is
  what it decays into once they stop — a camera that died five minutes
  ago should read "5 min old", not fall back to a rolling average of
  zero.
- **Clock skew**: age is computed from the frame's own EXIF capture
  timestamp against `time.time()` on the machine serving the debug UI,
  clamped at zero. Against a remote/mirrored camera this is only as
  good as the agreement between the two machines' clocks — an age of
  "0 ms" on a badly-skewed pair doesn't mean zero latency, it means the
  remote clock is ahead.

## WebRTC egress, ingest, and WHEP

`SpiriCamera.webrtc` is where every bit of aiortc lives; nothing else
in the package imports aiortc/FastAPI at module scope except the two
signaling doors that use it:

- {py:meth}`Camera.webrtc_offer <SpiriCamera.camera.Camera.webrtc_offer>`
  — a single `@remote_method`, reachable over zenoh like any other RPC.
- {py:mod}`SpiriCamera.whep` — a standalone FastAPI app implementing
  WHEP (`POST /whep`, `DELETE /whep/{session_id}`), independent of
  `ui.py`'s NiceGUI app, run via `SpiriCamera whep-serve`.

Both call the same `SpiriCamera.webrtc._negotiate`, so a viewer that
arrives over zenoh and one that arrives over HTTP land in the same
session registry and get the same video track and tags channel — the
signaling transport is the only thing that differs.

**The shared background event loop.** An `RTCPeerConnection` has to
keep running after the call that created it returns, but the zenoh RPC
path calls into `webrtc_offer` synchronously from a queryable callback
thread with no event loop at all. `SpiriCamera.webrtc.get_loop()`
starts one daemon thread running its own loop forever, the first time
anything asks for it, and every peer connection this module creates
lives there for good. `negotiate_sync` blocks the calling thread for a
result; `negotiate_async` is the same call awaited from a caller that
already has a loop (uvicorn's, in `whep.py`).

**Tags only travel one way.** A single SDP answer can only describe
`m=` sections the offer already listed — an answerer cannot add a data
channel that was not in the offer without a second negotiation round.
So the *viewer* has to create the data channel (labelled
`spiricamera-tags`) before sending its offer; `SpiriCamera.webrtc`
listens for it arriving via the `"datachannel"` event and, if it never
does, simply sends video with no tags. This means a generic WHEP player
(a browser, `ffplay`) gets video only; only a SpiriCamera-aware client
gets the per-frame `{"timestamp": ..., "tags": {...}}` JSON, read
straight out of that frame's EXIF via `SpiriCamera.exif.extract` — the
same tags already embedded per the "Frame-derived fields never travel
as their own field" rule above, not a second copy of them.

**Ingest is a source handler.** `SpiriCamera.sources.webrtc.WHEPSource`
is the mirror image: a `whep+http://host/path` or `whep+https://...`
source string (compound schemes, since `NetworkSource` already claims
bare `http`/`https`) pulls a remote WHEP publisher's stream in as an
ordinary `SourceBase`, negotiating on `open()` and reading decoded
frames off a background task on the same shared loop.

### Hooking a `whep-serve` camera up to MediaMTX

[MediaMTX](https://github.com/bluenviron/mediamtx) can pull a WHEP
stream directly as a path source — no WHIP push, no extra code on our
side. Point a path at whatever `SpiriCamera whep-serve` is listening
on:

```yaml
# mediamtx.yml
paths:
  spiricamera:
    source: whep://127.0.0.1:8889/whep
```

MediaMTX acts as the WHEP *client* here (the same role our own
`SpiriCamera.sources.webrtc.WHEPSource` plays), and re-serves the
video to as many RTSP/HLS/WebRTC/etc. viewers as you like from that
one path.

**The `spiricamera-tags` data channel does not survive this hop.**
MediaMTX's WHEP-pull offer only asks for a recvonly video track — no
data channel — so per the one-way rule above (offer decides which
`m=` sections can exist), our answer never includes one, and there is
nothing to carry the tags through. This isn't a bug to work around: as
of the current release, MediaMTX's WebRTC implementation has no data
channel support at all (checked its full config reference for
`datachannel`/`metadata`/`sei` — nothing), so no WHEP offer it sends
will ever open one. A viewer that needs tags has to talk to
`whep-serve` directly, bypassing MediaMTX, so its own offer can open
the channel.

The one metadata path MediaMTX *does* carry end-to-end is KLV — it
implements RFC 6597 (`SMPTE336M/90000`) as a first-class RTSP track
type, cross-muxing between MPEG-TS/SRT ingest and RTSP output. That
would mean encoding our tags as MISB ST 0601 KLV and giving
SpiriCamera an RTSP/SRT *output* carrying a synchronous KLV track —
unrelated to WHEP/WebRTC entirely, and not implemented yet (see
`todo.md`).

## Where things are tested

`tests/` exercises the source handlers and `Camera` against real
`SpiriSynq` peers over a namespaced zenoh session (see recent history
around "namespace the test suite's zenoh peer" for why — tests must not
collide with a zenoh session already running on the same machine). Run
the full suite with:

```console
python -m pytest
python -m pytest --cov=SpiriCamera
```

When adding a new source handler, add a corresponding test module
alongside the existing ones rather than folding it into
`test_camera.py` — handler tests exercise `open`/`close`/`read` in
isolation from the capture thread, which is what makes it possible to
test protocol edge cases without racing a background thread.
