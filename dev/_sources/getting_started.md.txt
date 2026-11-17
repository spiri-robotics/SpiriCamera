# Getting Started

This guide walks you through installing, configuring, and running SpiriCamera.

## Installation

Add SpiriCamera to your project:

```console
uv add SpiriCamera
```

Or install from source:

```console
uv sync
uv lock
```

## Configuration

SpiriCamera runs with sensible defaults. If you need to customize, set environment variables with the `SPIRICAMERA_` prefix:

```console
export SPIRICAMERA_ENVIRONMENT=production
export SPIRICAMERA_LOG_LEVEL=DEBUG
```

Or create a `.env` file in your project root. See the {ref}`api-reference` for all available options.

## Camera Usage

SpiriCamera provides a {py:class}`~SpiriCamera.Camera` class for live frame capture. A camera is a `SyncableObject`: it behaves like a small daemon that owns a device, captures in the background, and publishes each frame over SpiriSynq — not a handle you construct, read from synchronously, and throw away. The protocol detail of reaching a device lives in the source handlers under {py:mod}`SpiriCamera.sources`.

There are two roles, and most code only ever needs one of them:

- **Authoritative** — the process that actually owns the device (typically `SpiriCamera run`, or something like it). It constructs a `Camera` normally, `start()`s it, and lets the background thread do the work.
- **Mirror** — everyone else. Rather than constructing a second `Camera` pointed at the same device, get a handle onto the *existing* one over SpiriSynq and read its synced fields as they arrive.

### Consuming an existing camera (the common case)

Attach to a camera another process is already running by its SpiriSynq topic:

```python
from SpiriCamera import Camera

cam = Camera.from_topic("some-host/cameras/1/raw")
```

This gives you a mirror: same synced fields (`status`, `running`, `max_width`, ...), kept up to date by SpiriSynq, but it never opens a device locally — it exists to observe, not to capture. There is no `read()` to call.

A mirror receives settings, not frames. To get frames, choose a transport by subscribing to it:

| Transport | Subscribe with | Frames are | Suits |
| --- | --- | --- | --- |
| Raw | `cam.transport_raw_subscribe()` | {py:class}`~SpiriCamera.transports.RawFrame`, with a read-only BGR `array` | The same host, or a fast network |
| JPEG | `cam.transport_jpeg_subscribe()` | `bytes` | Most things, and the simplest to use |
| Video | `cam.transport_video_subscribe()` | {py:class}`~SpiriCamera.transports.VideoPacket`, H.264 | Constrained links |

Subscribing is what makes the camera run: with no subscribers on any transport, its source is closed. Unsubscribe when you're done (`cam.transport_jpeg_unsubscribe()`), or close the mirror. {doc}`data_model` shows the whole structure, and what gets sent when.

Then get frames one of two ways:

**Poll**, when you're already on a timer or a render loop (this is what the debug UI does, ticking in step with `max_framerate`):

```python
cam.transport_jpeg_subscribe()
frame_jpeg = cam.transport_jpeg.data  # the most recent frame; empty until the first arrives
```

**Or connect to its signal**, when you want to react the moment a new frame lands:

```python
def on_frame(frame_jpeg: bytes) -> None: ...


cam.transport_jpeg.data_changed.connect(on_frame)
cam.transport_jpeg_subscribe()
```

Both approaches work the same way whether `cam` is a mirror or the authoritative camera itself. Video has `cam.transport_video.packet_received` instead, as there is no single current frame to read. Other fields have the psygnal event every synced field gets, such as `cam.events.status`.

### Building on an existing camera

A mirror is a view of someone else's camera: its settings and overlays are that camera's, and changing them changes it for everyone. To get a *different* stream out of it — smaller, with your own overlays, at your own quality or framerate — use it as the source of a camera of your own instead:

```python
from SpiriCamera import Camera

small = Camera(
    "spirisynq://some-host/cameras/1/raw",
    max_width=640,
    max_height=360,
    quality=60,
)
small.start()
```

`spirisynq://` takes the same topic `Camera.from_topic` does, absolute or relative to this host's base topic: `spirisynq://./cameras/1/raw` is this host's `cameras/1/raw`. The scheme also picks which of the upstream's transports to pull: `spirisynq://` and `spirisynq+jpeg://` take JPEG, `spirisynq+raw://` takes raw with no decode, and `spirisynq+video://` takes H.264 for a constrained link.

Each upstream frame is scaled down to fit inside `max_width` x `max_height` (keeping its aspect ratio; it never scales up), then goes through this camera like any source's frame: its own `overlay_widgets` are drawn on, it is encoded at its own settings, and it is published on its own topic. The upstream is untouched, so any number of these can hang off one device. Frame tags carry over too — see {doc}`exif`.

This camera pulls from its upstream only while something subscribes to it, so demand travels up the chain: with nobody watching, the device at the top is closed.

The upstream does not have to exist yet, and may come and go: the camera waits for the topic, and reconnects when an upstream that shut down, crashed, or restarted is back — see "Waiting for a Source" below.

### Running a device yourself

If your process *is* the one meant to own the device — a standalone script, or something like the `SpiriCamera run` CLI command — construct a `Camera` normally and start it. Leave `start()` on its default `background=True`: the capture thread runs its own loop, paced to `max_framerate`, whenever something wants frames. Don't drive it frame-at-a-time yourself — a `Camera` is meant to be treated like a small daemon, not a handle you poll by calling `read()` in your own loop.

```python
from SpiriCamera import Camera

cam = Camera("/dev/video0", quality=85)
cam.start()  # reads what the device is, closes it, and waits for subscribers

cam.transport_jpeg_subscribe()  # something wants frames, so the device opens
frame_jpeg = cam.transport_jpeg.data  # whatever has been captured so far

cam.stop()
```

or as a context manager, which starts on entry and stops on exit:

```python
with Camera("/dev/video0", quality=85) as cam:
    cam.transport_jpeg.data_changed.connect(on_frame)
    cam.transport_jpeg_subscribe()
    ...
```

A subscriber anywhere counts, on this camera or across the network, so `SpiriCamera run` sits idle until someone asks for frames.

To change the rate frames are captured at, set `cam.max_framerate` (live — takes effect on the next frame, no restart needed) rather than calling `read()` less often. `start(background=False)` exists — it opens the device without the capture thread, leaving you to call `read()` yourself — but that's an edge case for tests and one-off scripts, not the pattern to reach for; ordinary code, including anything long-running, should stay on the default background thread, subscribe to a transport, and read its `data` or connect to its `data_changed`.

### Inspecting a Source

Source strings are resolved by handlers that register themselves. To inspect one without opening any hardware:

```python
from SpiriCamera import describe_source, known_schemes

info = describe_source("rtsp://host/stream")
print(info.scheme, info.target, info.handler)

print(known_schemes())
```

A camera whose source string cannot be resolved does not raise on construction; the reason lands in `cam.source.error` and `start()` is what refuses. This keeps a camera safe to bind to a live-edited input field.

### Network Streams

```python
# RTSP stream
cam = Camera("rtsp://admin:pass@192.168.1.100:554/stream")
cam.start()

# HTTP image stream (MJPEG)
cam = Camera("http://10.0.0.5/image.jpg")
cam.start()
```

### Files

A video file or a still image can stand in for a camera. Files are just
paths — absolute or relative, no scheme needed:

```python
cam = Camera("/srv/clips/approach.mp4")  # loops at the end
cam = Camera("./pattern.png")  # a steady picture
```

Use `file://` when a path would otherwise read as a URL. POSIX collapses
repeated slashes, so `rtsp://clip.mp4` is a real path to `clip.mp4`
inside a directory named `rtsp:` — and a stream URL. The stream wins;
`file://rtsp://clip.mp4` says you meant the file.

### Local Devices

A camera index, a device node, or an explicit scheme:

```python
cam = Camera("0")  # camera index, the OpenCV convention
cam = Camera("/dev/video0")  # device node
cam = Camera("v4l:///dev/video5")  # not plugged in yet
```

A schemeless path is recognised as a camera if the filesystem says it is
one, or if it is named like one — `/dev/video<N>`, or anything under
`/dev/v4l/` (the `by-id` links are the robust way to name a USB camera).
So `/dev/video0` works even before the device is plugged in, while
`/dev/null` and a half-typed `/dev/vi` do not. `v4l://` accepts any path.

### Waiting for a Source

A started camera does not give up on a source that is not there. A
device that is unplugged, or a `spirisynq://` topic nobody publishes
yet, leaves a wanted camera `running` with a status of
`waiting for source: <reason>`, retrying about once a second until it
works. The same happens if the device is unplugged or the upstream
camera goes away mid-stream: it waits, and picks up again when it is
back. `SpiriCamera run` therefore keeps running through a camera
being replugged or a host rebooting. Only `start(background=False)` —
scripted use with no capture thread to wait on — still raises instead.

### Knowing What It Is Doing

`camera.status` says what the camera is doing in words — `running`,
`stopped`, `idle: no subscribers`, waiting, or the reason it is none of those:

```python
cam = Camera("/dev/vi")  # half-typed
cam.status  # "Unrecognised source: '/dev/vi'. Supported: ..."

cam = Camera("v4l:///dev/video99")
cam.start()  # does not raise
cam.transport_jpeg_subscribe()
cam.status  # "waiting for source: Failed to open capture source: 'v4l:///dev/video99'"
```

`running` says whether anything wants frames, so whether the camera is
trying; `status` says whether frames are actually flowing, and why not
when they are not. `running` is set by the camera from its subscribers:
`start()` and `stop()` are the local switch, and nothing remote can turn
a camera off.

### Frame Tags

Each captured frame carries its own metadata, in its EXIF as a JPEG and
beside it as raw or video:
when it was taken, what took it, and anything you care to add — see the
{doc}`exif` guide for how to read tags back, add your own (GPS included),
and share the tagging job between more than one piece of software.

### Overlays

A camera can bake a small SVG "HUD" onto every frame, after resizing it
and before any transport — a resolution/framerate readout, a battery level, anything
templated from live data. The built-in metrics overlay is the
quickest way to see this working:

```python
from SpiriCamera import Camera
from SpiriCamera.overlay import camera_metrics_widget

cam = Camera("/dev/video0")
widget = camera_metrics_widget(cam)  # publishes its own topic
cam.overlay_widgets[widget.synq_absolute_path] = {}  # opt this camera into it
cam.start()
```

Every transport now includes the overlay, composited fresh on every
frame, on the resized image. For a feed without it, make a second camera
with this one as its source and draw on that one instead. Removing the entry (`del cam.overlay_widgets[widget.synq_absolute_path]`) drops
it again immediately — no restart needed.

A widget you author yourself is a {py:class}`~SpiriCamera.overlay.HudWidget`:
an SVG template, MiniJinja-rendered against the current frame's `exif`
tags, its actual `frame` (as-received width/height/framerate), and any
other live SpiriSynq object it declares under `objects`:

```python
from SpiriCamera.overlay import HudWidget

widget = HudWidget(
    synq_topic="my-hud",
    synq_authoritive=True,
    anchor="top_right",
    svg_template="""
        {# object: battery = azrael/battery_monitor #}
        <svg xmlns="http://www.w3.org/2000/svg" width="160" height="30">
          <text x="4" y="20" font-size="16" fill="white">
            {{ objects.battery.voltage }}V @ {{ frame.framerate }}fps
          </text>
        </svg>
    """,
)
cam.overlay_widgets[widget.synq_absolute_path] = {}
```

The key is the widget's `synq_absolute_path`, which includes the host prefix. A camera
can't find a widget by its bare `synq_topic`.

The `{# object: battery = ... #}` comment declares both the alias used
in the template and the default topic it mirrors — every camera that
opts into this widget gets that binding unless it overrides it for
itself:

```python
cam.overlay_widgets[widget.synq_absolute_path] = {
    "bindings": {"battery": "some-other-host/battery_monitor"},
    "x": 2,
    "y": 2,  # percent-of-frame position override, this camera only
}
```

A widget you author is not owned by any one camera — publish it once
and any number of cameras can mirror it, each with its own binding and
placement overrides. See {py:mod}`SpiriCamera.overlay` (or the
{ref}`overlay architecture notes <the-overlay-layer>`) for the full
templating rules, including why `frame` is a separate namespace from
`objects`.

### Configuration

Camera parameters are configured via environment variables:

```console
export SPIRICAMERA_SOURCE="rtsp://host/stream"
export SPIRICAMERA_QUALITY=90      # JPEG quality [1-100]
export SPIRICAMERA_FRAME_WIDTH=1280 # Most pixels wide a published frame is
export SPIRICAMERA_FRAME_HEIGHT=720 # ...and high; bigger frames are shrunk to fit
export SPIRICAMERA_FRAMERATE=30     # Capture framerate
export SPIRICAMERA_RTSP_PORT=8554   # `run` serves RTSP on this TCP port (0: off)
export SPIRICAMERA_RTSP_UDP_PORT=8000 # ...and the first of four UDP ports
export SPIRICAMERA_H264_PRESET=veryfast # x264 preset of the camera's one video encode
```

However many video subscribers and RTSP viewers a camera has, it is
encoded to H.264 once, in its own process, at the rate the worst link
allows. See {doc}`data_model` for how, {doc}`rtsp` for viewing it, and
{doc}`mediamtx` for translating it for other readers.

Or passed directly to the {py:class}`~SpiriCamera.Camera` constructor:

```python
cam = Camera(
    source="/dev/video0",
    quality=85,
    max_width=1280,
    max_height=720,
    max_framerate=30,
)
```

### CLI Usage

List the registered source handlers and the schemes they claim:

```console
SpiriCamera sources
```

Validate a source and report what the device can do:

```console
SpiriCamera validate /dev/video0
```

Capture frames to a file:

```console
SpiriCamera capture "rtsp://host/stream" --frames=10 -o frames.jpg
```

Run a camera on SpiriSynq until interrupted, capturing whenever something subscribes, and
serve it over {doc}`RTSP <rtsp>`:

```console
SpiriCamera run /dev/video0
```

`run` publishes three cameras under this host's base topic:

| Topic | What it is |
| --- | --- |
| `cameras/1/raw` | The source itself, within `SPIRICAMERA_FRAME_WIDTH` x `SPIRICAMERA_FRAME_HEIGHT`. |
| `cameras/1/hud` | `raw`, with the widgets `SPIRICAMERA_HUD_WIDGETS` names, by topic, separated by spaces or commas. |
| `cameras/1/preview` | `raw` at 240 pixels high and one frame a second, for picking a camera from a list. |

`hud` and `preview` take `raw`'s raw frames in the same process, so each runs, and runs
`raw`, only while something subscribes to it. `SPIRICAMERA_CAMERA_NAME` replaces the
`1`, and can be any text, such as `belly`. Give each camera on a host its own name.
`SPIRICAMERA_TOPIC` replaces the whole of `cameras/1`. RTSP serves each of them, as
`/raw`, `/hud` and `/preview`; see {doc}`rtsp`.

The source argument is optional everywhere; without it, `SPIRICAMERA_SOURCE` is used.

## Quick Start

Run the CLI:

```console
SpiriCamera --help
```

For verbose output, which goes before the command:

```console
SpiriCamera --verbose run /dev/video0
```

## Next Steps

- Read the {ref}`api-reference` for programmatic details
- View a camera in QGroundControl, or from any other RTSP client, with {doc}`rtsp`
- Check the [pyproject.toml](https://github.com/spiri-robotics/SpiriCamera/blob/main/pyproject.toml) for build and dependency configuration
- See the [Docker compose files](https://github.com/spiri-robotics/SpiriCamera/blob/main/docker/compose.yaml) for deployment options
