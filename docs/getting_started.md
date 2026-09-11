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

SpiriCamera provides a :py:class:`~SpiriCamera.Camera` class for live frame capture. A camera owns the capture lifecycle, encoding, and publication; the protocol detail of reaching a device lives in the source handlers under :py:mod:`SpiriCamera.sources`.

### Basic Usage

```python
from SpiriCamera import Camera

# Create a camera reader
cam = Camera("/dev/video0", quality=85)

# Start capture: opens the device and begins a background capture
# thread that publishes each frame to cam.image
cam.start()

# The most recent encoded frame, updated in the background
frame_jpeg = cam.image

# Or drive frames yourself
frame_jpeg = cam.read()          # capture, encode, publish, return bytes
frame_bgr = cam.read_frame()     # the raw BGR numpy array

# Stop when done
cam.stop()
```

A camera is also a context manager:

```python
with Camera("testimage://", max_width=1280, max_height=720) as cam:
    frame_jpeg = cam.read()
```

Pass `background=False` to `start()` to open the device without a capture thread, when you want to drive `read()` yourself.

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

### Configuration

Camera parameters are configured via environment variables:

```console
export SPIRICAMERA_SOURCE="rtsp://host/stream"
export SPIRICAMERA_QUALITY=90      # JPEG quality [1-100]
export SPIRICAMERA_FRAME_WIDTH=1280 # Capture width
export SPIRICAMERA_FRAME_HEIGHT=720 # Capture height
export SPIRICAMERA_FRAMERATE=30     # Capture framerate
```

Or passed directly to the :py:class:`~SpiriCamera.Camera` constructor:

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
