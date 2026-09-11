# SpiriCamera

Work with Cameras in SpiriSynq

## Getting Started

### Installation

```console
# Install dependencies
uv sync

# Generate lock file
uv lock
```

### Run

```console
SpiriCamera --help
```

### Run as module

```console
python -m SpiriCamera
```

## Camera Usage

### Python API

```python
from SpiriCamera import Camera

cam = Camera("/dev/video0", quality=85)
cam.start()
frame_bytes = cam.read()
cam.stop()
```

### CLI

```console
# Validate a source
SpiriCamera validate --source "/dev/video0"

# Capture frames
SpiriCamera capture --source "rtsp://host/stream" --frames=10 -o out.jpg

# Run live capture
SpiriCamera run --source "/dev/video0"
```

### NiceGUI Web UI

```console
uv run python -m SpiriCamera.ui
```

Install the optional UI dependency:

```console
uv pip install SpiriCamera[ui]
```

## Development

```console
# Run tests
python -m pytest
python -m pytest --cov=SpiriCamera

# Run type checking
python -m mypy src/ 2>/dev/null
```

## Documentation

```console
# Install docs dependencies
uv sync
python -m pip install -r docs/requirements.txt

# Build HTML docs
sphinx-build -b html docs docs/_build

# View docs locally
python -m http.server --directory docs/_build 8888
```


## License

Licensed under the GNU General Public License v3.0 or later.

