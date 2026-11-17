# AGENTS.md

SpiriCamera: Work with Cameras in SpiriSynq. Python >=3.13, managed with `uv` (`uv.lock` is
committed). Generated from the SpiriProjectTemplates copier template; `uvx copier update` pulls
in template changes.

## Commands

- Everything CI checks: `make check`.
- Format and autofix: `make format`.
- Tests: `uv run pytest`.
- Docs: `make docs` (Sphinx, MyST markdown; warnings are errors), `make pdf` for the controlled
  PDF, `make lint-docs` for prose (Vale). HTML is published to GitHub Pages from `main`.

## Conventions

- The docs are a controlled document (spiri-docs, as for every Spiri manual): `revisions.yaml`
  is the only place the document's revision is written. Revise it when what the docs say
  changes; it's separate from the package version.
- The app talks to the robot over SpiriSynq. In docker it joins the `spirisynq` network, where
  the zenoh-router app connects it to the rest of SpiriSynq; never put it on the host network.
- The template owns the versions it ships (base dependencies, workflow actions, the uv image)
  and bumps them weekly; take them with `uvx copier update` and `uv lock`, not by hand.
- Every change gets a categorized entry in `CHANGES.md` (Features / Fixes / Improvements /
  Tests / Internal) under `## Unreleased`; commits use conventional-commit style (`feat:`,
  `fix:`, `refactor:`).

## Structure

```
src/SpiriCamera/
    camera.py          # CameraBase (synced state) + Camera (capture thread, SpiriSynq sync)
    transports.py      # transport_raw/jpeg/video objects, wire formats, DataChannel, RateControl
    video.py           # The camera's one H.264 encode and worst-subscriber rate control
    rtsp.py            # RTSP server: H.264 and KLV over RTP, viewers' RTCP to rate control
    klv.py             # MISB ST 0601 local sets
    cli.py             # typer CLI app  →  version, sources, validate, run, capture
    main.py            # Settings (SPIRICAMERA_ env prefix), setup_logging, main()
    ui.py              # NiceGUI debug UI (the camera-debug-ui app)
    web.py             # unused stub from the old template; the UI is ui.py
    overlay.py         # SVG HUD widgets, MiniJinja templating, thorvg rasterization
    exif.py            # EXIF tag embed/extract in JPEG UserComment
    sources/
        base.py        # SourceBase, SourceURL, CaptureSettings, SourceCapabilities
        capture.py     # Shared OpenCVSource base
        v4l.py         # v4l://, v4l2://  — local V4L2 devices (Linux)
        network.py     # rtsp://, rtmp://, http(s)://
        file.py        # file://  — video files or still images, looping
        testimage.py   # testimage://  — bundled SVG test patterns
        spirisynq.py   # spirisynq://  — frames from another camera over SpiriSynq
```

Package root: `src/SpiriCamera/`. CLI entry: `SpiriCamera` (from `[project.scripts]`).

## Import / Source Registration

Modules in `sources/` register themselves simply by being imported via `__init__.py`. No registry dict to maintain.

**Rule:** `camera.py` imports `sources/`, but `sources/` must never import `camera.py`.

## SpiriSynq Quirks

- `CameraBase` is a `SyncableObject`. All fields (including "read-only" ones like `received_width`) are technically writable from a remote peer.
- `Camera` is authoritative by default (`synq_authoritive=True`); `Camera.from_topic()` creates mirrors (`synq_authoritive=False`). Authoritative flag controls topic hostname prefix and whether `@remote_method` RPCs are exposed.
- Frame-derived fields (`received_width`, `received_height`, `received_ratio`, `received_framerate`, `exif_tags`, `exif_timestamp`) are excluded from sync via `CameraBase.synq_skip_sync`. They are read back from each frame, never sent as separate sync fields.
- Frames are not fields. They travel on `transport_*/@data*` verbatim keys that `**` never matches, so a mirror selects a transport by subscribing to it. `data` is a property with a `data_changed` signal; don't make it a dataclass field.
- A camera runs only while something subscribes to a transport (`running` is set by the camera, not by peers). Demand travels up `spirisynq://` relay chains. `docs/data_model.md` has the whole structure as annotated YAML; keep it current when fields or keys change.
- Frame tags travel in a JPEG's `UserComment` as JSON, or in the JSON attachment of a raw frame or video packet.
- QoS on `@` keys is SpiriSynq's `<field>_qos` convention: sender-local class attributes, never synced.

## Overlay Rendering (thorvg)

thorvg's SVG parser has documented gaps:
1. `dy` on `<text>` is silently ignored — bake offsets into `y` itself.
2. `stroke`, `stroke-width`, `paint-order` on `<text>` are ignored — fake outlines with layered fills.
3. Text baseline differs from browsers. Apply the correction constants in `overlay.py`: `_TEXT_BASELINE_SLOPE = 0.1915`, `_TEXT_BASELINE_INTERCEPT = -1.074`.

A bundled `Miracode.ttf` is loaded once at import time via `thorvg.Engine.font_load()`.

## RTSP

The cameras' way out to anything that isn't SpiriSynq. Each camera `run` publishes has two mounts: `/<camera>` (`/raw`, `/hud`, `/preview`) is UDP only and its viewers' RTCP drives that camera's rate control; `/<camera>/relay` is TCP only, for MediaMTX on the same host, and has no say. MediaMTX is the edge translator for STANAG 4609 output and browsers, unpatched.

`LossMeter` turns RTCP into reports without changing `RateController`: loss comes from NACKs, so retransmission-repaired loss still lowers the bitrate, on purpose. Keep each track's CNAME distinct: a shared one stops QGroundControl decoding. Test changes here against real QGroundControl, because GStreamer's behaviour is what shaped it.

## UV Dependency Trick

`pyproject.toml` uses:
```toml
override-dependencies = ["opencv-python; sys_platform == 'never'"]
```
This voids the `opencv-python` (GUI variant) pulled in by `ultralytics` (examples extra), resolving the conflict with `opencv-python-headless`. Do not remove it.

## Testing

- pytest config in `pyproject.toml`: `pythonpath = ["."]`, `asyncio_mode = "auto"`
- No coverage threshold configured
- `yolo_demo` test images come from `ultralytics` package assets at `ultralytics/assets/bus.jpg` (not bundled)

## Apps

The one image runs as two SpiriConfig apps, each with its own compose file, mirrored in
spiri-robotics/Appstore-Spiri_development:

- `camera-ingress/compose.yaml`: `SpiriCamera run`, capturing, publishing onto SpiriSynq as
  `cameras/<SPIRICAMERA_CAMERA_NAME>/{raw,hud,preview}` (hud and preview relay raw in-process),
  and serving `raw` over RTSP on published ports.
- `camera-debug-ui/compose.yaml`: `python -m SpiriCamera.ui`, a SpiriConfig plugin page.

`docker/compose.yaml` runs the ingress from the published image; with `compose.dev.yaml`, from
this checkout.
