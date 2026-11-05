# Changes

## Unreleased

## 0.2.2 (2026-11-05)

### Fixes

- A `spirisynq://` source takes a topic relative to this host's base topic,
  `spirisynq://./camera1`, as SpiriSynq topics do elsewhere. It never found its upstream:
  the relative topic was compared, unresolved, against the absolute ones published.

## 0.2.1 (2026-11-05)

### Fixes

- A network stream that stops delivering frames is reconnected after 5 seconds. An RTSP
  server that dropped the session left the camera frozen on its last frame until restarted,
  each read waiting out FFmpeg's 30-second timeout.
- A HUD widget's template, malformed part way through a live edit, no longer stops the
  camera publishing frames; that widget is skipped until the template parses.
- An overlay widget or object topic that does not resolve is retried every 2 seconds, not
  every frame. Each attempt blocked the capture thread for SpiriSynq's retries, dropping the
  camera to a few frames a second while a widget was missing.

### Improvements

- The image is built from SpiriProjectTemplates' Dockerfile: uv pinned, a `dev` stage the
  tests run in, and the camera-debug-ui's NiceGUI installed as a regular dependency rather
  than the `ui` extra.
- `docker/compose.yaml` runs the camera (`SpiriCamera run`, defaulting to the test pattern) on
  the `spirisynq` network, named after the host, instead of the old template's placeholder.
- Docs are published to GitHub Pages instead of Read the Docs, and are a controlled document
  (spiri-docs): a document number and revision, and a PDF attached to every release.

### Internal

- Adopted SpiriProjectTemplates (`.copier-answers.yml`): its CI, ruff config, Makefile and
  weekly template update. The `docs` extra is now a dependency group.
- Code formatted and linted to the template's ruff config.
