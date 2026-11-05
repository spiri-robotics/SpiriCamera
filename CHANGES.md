# Changes

## Unreleased

### Features

- `SpiriCamera run` serves the camera for WHEP playback too, at `/whep` on
  `SPIRICAMERA_WHEP_PORT`, so MediaMTX or a cloud can pull it with no separate `whep-serve`
  or mirror process.
- `SPIRICAMERA_H264_PRESET` picks x264's speed preset for WebRTC/WHEP, `veryfast` by
  default. aiortc left it at x264's `medium`, over two cores of a Raspberry Pi 4 at 720p30;
  `veryfast` takes about one, `ultrafast` half.

### Improvements

- A camera is encoded to H.264 once, however many WebRTC/WHEP viewers it has: every session
  shares one encoder, which suits the worst viewer's link (lowest bitrate, frame rate and
  height). Each session had its own encoder before. A viewer that joins, falls behind or
  asks for one gets a keyframe.
- A mirror no longer publishes WebRTC itself, which was a second encode of the camera. A
  WHEP app over a mirror (cloud-link's camera-whep) now only signals: the offer goes to the
  camera's own process over SpiriSynq (`webrtc_offer`, and a new `webrtc_close`), which
  answers and sends the video.
- WebRTC is H.264 only: an offer without H.264 is refused, rather than given VP8.

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
