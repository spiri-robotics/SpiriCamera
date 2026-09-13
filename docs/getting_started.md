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

cam = Camera.from_topic("some-host/dev-video0")
```

This gives you a mirror: same synced fields (`image`, `status`, `running`, ...), kept up to date by SpiriSynq, but it never opens a device locally — it exists to observe, not to capture. There is no `read()` to call. Get frames one of two ways:

**Poll**, when you're already on a timer or a render loop (this is what the debug UI does, ticking in step with `max_framerate`):

```python
frame_jpeg = cam.image   # the most recent frame, updated for you in the background
```

**Or subscribe**, when you want to react the moment a new frame (or any other field) lands, via the psygnal event every synced field gets:

```python
def on_frame(frame_jpeg: bytes) -> None:
    ...

cam.events.image.connect(on_frame)
```

Both approaches work the same way whether `cam` is a mirror or the authoritative camera itself — `image` is just a field, and it changes the same way either way.

### Running a device yourself

If your process *is* the one meant to own the device — a standalone script, or something like the `SpiriCamera run` CLI command — construct a `Camera` normally and start it. Leave `start()` on its default `background=True`: the capture thread runs its own loop, paced to `max_framerate`, for as long as the camera is running. Don't drive it frame-at-a-time yourself — a `Camera` is meant to be treated like a small daemon, not a handle you poll by calling `read()` in your own loop.

```python
from SpiriCamera import Camera

cam = Camera("/dev/video0", quality=85)
cam.start()               # opens the device, begins the background capture thread

frame_jpeg = cam.image    # whatever the background thread has captured so far

cam.stop()
```

or as a context manager, which starts on entry and stops on exit:

```python
with Camera("/dev/video0", quality=85) as cam:
    frame_jpeg = cam.image
```

To change the rate frames are captured at, set `cam.max_framerate` (live — takes effect on the next frame, no restart needed) rather than calling `read()` less often. `start(background=False)` exists — it opens the device without the capture thread, leaving you to call `read()` yourself — but that's an edge case for tests and one-off scripts, not the pattern to reach for; ordinary code, including anything long-running, should stay on the default background thread and read `cam.image` or connect to `cam.events.image`.

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
cam = Camera("/srv/clips/approach.mp4")   # loops at the end
cam = Camera("./pattern.png")             # a steady picture
```

Use `file://` when a path would otherwise read as a URL. POSIX collapses
repeated slashes, so `rtsp://clip.mp4` is a real path to `clip.mp4`
inside a directory named `rtsp:` — and a stream URL. The stream wins;
`file://rtsp://clip.mp4` says you meant the file.

### Local Devices

A camera index, a device node, or an explicit scheme:

```python
cam = Camera("0")                 # camera index, the OpenCV convention
cam = Camera("/dev/video0")       # device node
cam = Camera("v4l:///dev/video5") # not plugged in yet
```

A schemeless path is matched by asking the filesystem what it is, not by
how it is spelled, so `/dev/video0` is recognised as a camera and
`/dev/null` is not. That check needs the device to exist; write
`v4l://` if you are configuring one that will appear later.

### Knowing What It Is Doing

`camera.status` says what the camera is doing in words — `running`,
`stopped`, or the reason it is neither:

```python
cam = Camera("/dev/vi")     # half-typed
cam.status                  # "Unrecognised source: '/dev/vi'. Supported: ..."

cam = Camera("v4l:///dev/video99")
cam.start()                 # raises CameraError
cam.status                  # "Failed to open capture source: '/dev/video99'"
```

`running` says whether frames are flowing; `status` says why not when
they are not.

### Frame Tags

Each captured frame is a JPEG with its own metadata baked into its EXIF:
when it was taken, what took it, and anything you care to add.

```python
cam = Camera("/dev/video0")
cam.exif_update(mission="probe-1")
cam.start()

cam.exif_tags       # {'timestamp': '1789...', 'make': ..., 'mission': 'probe-1'}
cam.exif_timestamp  # Unix timestamp of the frame currently in cam.image
```

`exif_tags` and `exif_timestamp` are read back *out of* `cam.image`, and
so are `received_width`, `received_height` and `received_ratio`. None of
them is sent over SpiriSynq — they are properties, not synced fields, so
there is nothing there *to* send. That is on purpose: SpiriSynq publishes
each field independently and promises nothing about two of them arriving
together, so a timestamp sent beside an image is a timestamp a peer can
read against the wrong image. Travelling inside the JPEG, they cannot
come apart from it — a peer that receives a frame fills them in for
itself, exactly:

```python
mirror.image = frame          # the only thing that crossed the network
mirror.exif_timestamp         # when the far end took it
mirror.received_width         # what it actually sent
```

### Multiple Tag Providers

More than one piece of software may want to tag the same camera's
frames — a logger, a telemetry service, a human poking at the debug UI.
`exif_set_tags` gives each one its own namespace, so setting one never
touches another's:

```python
cam.exif_set_tags("telemetry", {"battery": "88"})
cam.exif_set_tags("logger", {"build": "42"})

cam.exif_clear_tags("telemetry")   # only "battery" goes away
```

Calling `exif_set_tags` again for the same provider *replaces* that
provider's tags rather than merging into them — drop a key by leaving it
out next time. A value of `""` is kept rather than dropped, which is how
a provider suppresses one of the built-in tags (say, omitting `source`
from a frame headed somewhere public). Providers merge over the built-ins
in sorted-name order, so the result does not depend on which one
registered first; two providers naming the *same* tag still resolve in
that order, which is worth avoiding with a distinguishing prefix rather
than relying on.

`exif_update(**tags)` is a shorthand for the common case of one caller
adding a tag or two with no provider name of its own — it merges into a
shared default bucket, and a value of `""` there removes just that tag:

```python
cam.exif_update(mission="probe-1")
cam.exif_update(mission="")   # removes it; other tags untouched
```

Two *different* callers both using `exif_update` still share that one
bucket and can still overwrite each other; once that matters, give each
one a provider name and use `exif_set_tags` instead.

To attach something the camera itself can compute, override the method
that builds the built-in tags — this still gets whatever providers have
set merged on top:

```python
class SurveyCamera(Camera):
    def exif_tags_for_frame(self, frame):
        tags = super().exif_tags_for_frame(frame)
        tags["gps"] = f"{self.fix.latitude},{self.fix.longitude}"
        return tags
```

Tag names are free-form; the whole set round-trips through the EXIF
`UserComment` field. Names that have a real EXIF equivalent — `make`,
`model`, `serial_number`, `software`, `description`, `datetime` — are
also written to that tag, so ordinary image tools show something
sensible.

Because a tag records a capture time, two captures of an unchanging
scene produce different bytes. Set `exif_enabled = False` for
byte-identical frames, which psygnal then suppresses rather than
republishing.

### Overlays

A camera can bake a small SVG "HUD" onto every frame before it is
encoded — a resolution/framerate readout, a battery level, anything
templated from live data. The built-in metrics overlay is the
quickest way to see this working:

```python
from SpiriCamera import Camera
from SpiriCamera.overlay import camera_metrics_widget

cam = Camera("/dev/video0")
widget = camera_metrics_widget(cam)          # publishes its own topic
cam.overlay_widgets[widget.synq_topic] = {}   # opt this camera into it
cam.start()
```

`cam.image` now includes the overlay, composited fresh on every frame.
Removing the entry (`del cam.overlay_widgets[widget.synq_topic]`) drops
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
cam.overlay_widgets[widget.synq_topic] = {}
```

The `{# object: battery = ... #}` comment declares both the alias used
in the template and the default topic it mirrors — every camera that
opts into this widget gets that binding unless it overrides it for
itself:

```python
cam.overlay_widgets[widget.synq_topic] = {
    "bindings": {"battery": "some-other-host/battery_monitor"},
    "x": 2, "y": 2,   # percent-of-frame position override, this camera only
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
export SPIRICAMERA_FRAME_WIDTH=1280 # Capture width
export SPIRICAMERA_FRAME_HEIGHT=720 # Capture height
export SPIRICAMERA_FRAMERATE=30     # Capture framerate
```

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

Run live capture, publishing to SpiriSynq until interrupted:

```console
SpiriCamera run /dev/video0
```

The source argument is optional everywhere; without it, `SPIRICAMERA_SOURCE` is used.

## Quick Start

Run the CLI:

```console
SpiriCamera --help
```

Or run as a module:

```console
python -m SpiriCamera
```

For verbose output:

```console
SpiriCamera run --verbose
```

## Next Steps

- Read the {ref}`api-reference` for programmatic details
- Check the [pyproject.toml](https://github.com/spiri-robotics/spiri-app-template/blob/main/pyproject.toml) for build and dependency configuration
- See the [Docker compose files](https://github.com/spiri-robotics/spiri-app-template/blob/main/templates/) for deployment options
