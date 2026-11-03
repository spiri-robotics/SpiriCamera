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
    cli.py             # typer CLI app  →  version, sources, validate, run, capture, whep-serve
    main.py            # Settings (SPIRICAMERA_ env prefix), setup_logging, main()
    ui.py              # NiceGUI debug UI (the camera-debug-ui app)
    web.py             # unused stub from the old template; the UI is ui.py
    webrtc.py          # aiortc integration, shared daemon event loop
    whep.py            # FastAPI WHEP HTTP API (POST/DELETE /whep)
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
        webrtc.py      # whep+http(s)://  — pull remote WHEP publisher
```

Package root: `src/SpiriCamera/`. CLI entry: `SpiriCamera` (from `[project.scripts]`) or `uv run python -m SpiriCamera`.

## Import / Source Registration

Modules in `sources/` register themselves simply by being imported via `__init__.py`. No registry dict to maintain.

**Rule:** `camera.py` imports `sources/`, but `sources/` must never import `camera.py`.

## SpiriSynq Quirks

- `CameraBase` is a `SyncableObject`. All fields (including "read-only" ones like `received_width`) are technically writable from a remote peer.
- `Camera` is authoritative by default (`synq_authoritive=True`); `Camera.from_topic()` creates mirrors (`synq_authoritive=False`). Authoritative flag controls topic hostname prefix and whether `@remote_method` RPCs are exposed.
- Frame-derived fields (`received_width`, `received_height`, `received_ratio`, `received_framerate`, `exif_tags`, `exif_timestamp`) are excluded from sync via `CameraBase.synq_skip_sync`. They are read back from the JPEG byte image, never sent as separate sync fields.
- All EXIF tags travel inside the JPEG's `UserComment` field as JSON. Every node extracts tags from the image bytes.

## Overlay Rendering (thorvg)

thorvg's SVG parser has documented gaps:
1. `dy` on `<text>` is silently ignored — bake offsets into `y` itself.
2. `stroke`, `stroke-width`, `paint-order` on `<text>` are ignored — fake outlines with layered fills.
3. Text baseline differs from browsers. Apply the correction constants in `overlay.py`: `_TEXT_BASELINE_SLOPE = 0.1915`, `_TEXT_BASELINE_INTERCEPT = -1.074`.

A bundled `Miracode.ttf` is loaded once at import time via `thorvg.Engine.font_load()`.

## WebRTC

All `RTCPeerConnection` objects share a single daemon thread with a background asyncio event loop — started on first use, never stopped (so the synchronous zenoh-callback path `Camera.webrtc_offer` works).

**WHEP data channel constraint:** The viewer must create the `spiricamera-tags` data channel *before* sending its WHEP offer, otherwise it gets video-only only. MediaMTX has no data channel support.

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

- `camera-ingress/compose.yaml`: `SpiriCamera run`, capturing and publishing onto SpiriSynq.
- `camera-debug-ui/compose.yaml`: `python -m SpiriCamera.ui`, a SpiriConfig plugin page.

`docker/compose.yaml` runs the ingress from the published image; with `compose.dev.yaml`, from
this checkout.
