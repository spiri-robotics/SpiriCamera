# SpiriCamera

Work with Cameras in SpiriSynq

Documentation: https://spiri-robotics.github.io/SpiriCamera/

## Usage

From Python, attach to a camera another process is running, by its SpiriSynq topic:

```python
from SpiriCamera import Camera

cam = Camera.from_topic("some-host/dev-video0")
frame_jpeg = cam.image  # the latest frame, kept up to date in the background
```

Or own a device yourself:

```python
with Camera("/dev/video0", quality=85) as cam:
    frame_jpeg = cam.image
```

From the command line:

```console
SpiriCamera validate --source "/dev/video0"
SpiriCamera capture --source "rtsp://host/stream" --frames=10 -o out.jpg
SpiriCamera run --source "/dev/video0"
uv run python -m SpiriCamera.ui   # NiceGUI debug UI
```

## Apps

The image runs as two SpiriConfig apps, each with its own compose file:

- [camera-ingress/](camera-ingress/compose.yaml) captures from a source and publishes frames
  onto SpiriSynq.
- [camera-debug-ui/](camera-debug-ui/compose.yaml) is a SpiriConfig plugin page for inspecting
  a camera's frames and editing its overlays.

## Development

```console
uv sync          # creates .venv and uv.lock; commit uv.lock
make check       # what CI runs: lint, tests, docs
```

Run it in docker, built from this checkout:

```console
docker compose -f docker/compose.yaml -f docker/compose.dev.yaml up --build
```

It joins the `spirisynq` docker network, which the zenoh-router app creates. Without that app
installed, create the network by hand: `docker network create spirisynq`.

## Releasing

1. Move the `## Unreleased` entries in `CHANGES.md` under the new version.
2. Bump `version` in `pyproject.toml`, then `uv lock`.
3. Tag `vX.Y.Z` and push the tag. CI then:
   - publishes the image to `ghcr.io/spiri-robotics/spiricamera` as `X.Y.Z`,
     `X.Y` and `latest`;
   - attaches the docs' controlled PDF to the release.

## First-time GitHub setup

Docs are published to GitHub Pages by `.github/workflows/docs.yml`. Turn it on once, in
**Settings → Pages → Build and deployment → Source: GitHub Actions**.

## Template

Generated from [SpiriProjectTemplates](https://github.com/spiri-robotics/SpiriProjectTemplates).
Pull in template changes with `uvx copier update`.

## License

Licensed under the GNU General Public License v3.0 or later.
