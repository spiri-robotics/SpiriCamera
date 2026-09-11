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

SpiriCamera provides a :py:class:`~SpiriCamera.Camera` class for live frame capture from video sources, and a :py:class:`~SpiriCamera.CameraSource` class for parsing and validating source strings.

### Basic Usage

```python
from SpiriCamera import Camera

# Create a camera reader
cam = Camera("/dev/video0", quality=85)

# Start capture (opens device, detects capabilities)
cam.start()

# Read frames as JPEG bytes
frame_jpeg = cam.read()

# Or read as numpy arrays (BGR format)
frame_bgr = cam.read_numpy()

# Stop when done
cam.stop()
```

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

Validate a source without capturing:

```console
SpiriCamera validate --source "/dev/video0"
```

Capture frames to a file:

```console
SpiriCamera capture --source "rtsp://host/stream" --frames=10 -o frames.jpg
```

Run live capture:

```console
SpiriCamera run --source "/dev/video0"
```

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
