# Changes

## 0.4.0 (2026-11-25)

### Features

- `SPIRICAMERA_RELATIVE_TO` and `SPIRICAMERA_HUD_WIDGETS` take `./` for this host's base topic,
  so one setting, `./robot_gimbal1` or `./hud/widgets/horizon`, serves every robot. `run`
  writes the topic out in full, since a link must be absolute and a peer would read `./` as its
  own host. The `camera-ingress` app passes `RELATIVE_TO` through.

### Fixes

- A camera reading an RTSP stream no longer falls seconds behind it. The capture loop paced
  its reads to `max_framerate`, so the first pass that ran longer than a frame left it reading
  slower than the stream sent, never to catch up, and the rest queued in the UDP receive buffer
  until it overflowed: about 3 seconds of latency, and smeared frames from the packets dropped,
  on a Raspberry Pi 5 behind a 30 fps gimbal camera. Network streams and V4L2 cameras are now
  read on a thread of their own as fast as they send, the camera takes only the newest frame,
  and `max_framerate` drops the frames it doesn't want instead of leaving them to wait.

### Improvements

- Network streams open with `fflags;nobuffer`, and RTSP streams also with `max_delay;0` and a
  256 KB `buffer_size`, so a lost packet is skipped instead of waited for and a decoder that
  falls behind can't queue seconds of video. Options set in `OPENCV_FFMPEG_CAPTURE_OPTIONS`
  still win, key by key.

### Tests

- A 30 fps stream that queues what isn't read, behind passes longer than a frame: the
  published frame stays current. It was over half a second old, and getting older, before.

## 0.3.0 (2026-11-25)

### Features

- `SpiriCamera run` publishes three cameras, at `cameras/1/raw`, `cameras/1/hud` and
  `cameras/1/preview` under the host's base topic. `hud` is `raw` with whatever overlay widgets
  are added to it, and `preview` is `raw` at 96 pixels high, JPEG quality 70 and one frame a
  second, about 2.4 KB a frame, small enough to leave every camera's on a map. Both relay
  `raw` in the same process, and run only while something subscribes. `SPIRICAMERA_CAMERA_NAME`
  replaces the `1` with any text, and `SPIRICAMERA_HUD_WIDGETS` names the widgets on `hud`.
- `SpiriCamera run` serves its cameras over RTSP, on `SPIRICAMERA_RTSP_PORT` (8554) and four
  UDP ports from `SPIRICAMERA_RTSP_UDP_PORT` (8000), each sharing its camera's one H.264 encode. Each frame
  carries a MISB ST 0601 KLV local set on a second track. Each camera `run` publishes has two
  mounts: `/raw`, `/hud` and `/preview` are for viewers such as QGroundControl, whose RTCP drives
  that camera's rate control, and `/raw/relay` and the rest are for MediaMTX on the same host,
  which translates it for STANAG 4609 consumers and browsers. Loss that retransmission repairs
  still lowers the bitrate. The local set holds only the capture time and version so far.
- Docs: the camera's data structure as annotated YAML, using a camera from a C++ app over raw
  zenoh, with examples in `examples/cpp/`, and serving a camera through MediaMTX so that rate
  control and on-demand running keep working. The guides now describe the transports.
- `SPIRICAMERA_H264_PRESET` picks x264's speed preset for the camera's H.264 encode, `veryfast`
  by default. x264's own default, `medium`, takes over two cores of a Raspberry Pi 4 at 720p30;
  `veryfast` takes about one, `ultrafast` half.

### Improvements

- Breaking: `SpiriCamera run`'s camera is no longer named after its source
  (`spiricamera_dev_video0`), but `cameras/1/raw`, and `SPIRICAMERA_TOPIC` now replaces
  `cameras/1`, the topic the three cameras sit under, rather than naming the camera.
  Anything reading `<robot>/camera1` or `<robot>/hud/camera1`, such as cloud-link's
  `CLOUD_LINK_CAMERA`, needs the new topics.
- Breaking: WHEP and WebRTC are gone, in favour of RTSP: `whep-serve`, the `webrtc_offer` RPC, and
  the `whep+http://` and `whep+https://` sources. cloud-link's camera-whep relies on `webrtc_offer`,
  so it won't work with this release.
- A camera is encoded to H.264 once, however many readers it has: they share one encoder, which
  suits the worst link (lowest bitrate, frame rate and height). A reader that joins, falls behind
  or asks for one gets a keyframe.
- The H.264 encode starts from the frame the camera just encoded its JPEG from,
  rather than decoding that JPEG again: a quarter of a Raspberry Pi
  4 core at 720p30.
- A `spirisynq://` camera that shrinks its upstream's frames, as a HUD variant does, resizes
  bilinearly down to half size, area-averaging only below that: 2.3 ms a 720p frame on a
  Raspberry Pi 4 where area-averaging took 12.3.

### Fixes

- A widget whose SVG isn't well-formed XML is skipped and logged, like any widget that fails to
  render, instead of crashing the camera: thorvg segfaults on it. SpiriMav's rangefinder widget
  did this below its minimum range (an unescaped `<`), which had the HUD camera restarting.
- A widget template can read a declared object's nested fields, such as
  `objects.cam.transport_jpeg.quality`, so `camera_metrics_widget` renders again. A nested field
  keeps the value it had when the object was first mirrored, a limit of SpiriSynq's generic mirrors.
- A camera stopped mid-capture says `stopped`, rather than the capture failure its last read hit.
- A camera's frames are held to `max_width` x `max_height` (`SPIRICAMERA_FRAME_WIDTH`/
  `_HEIGHT`) whatever its source sends: a bigger frame is shrunk, keeping its aspect ratio,
  before it is drawn on and encoded. The size was only ever asked of the source, which
  FFmpeg ignores for a network stream, so an RTSP camera published its stream's own size.

### Tests

- The suite matches the demand-driven camera again: a camera on SpiriSynq runs only while
  subscribed, `running` is the camera's to set, frames aren't fields, and a relay waits for its
  upstream's first frame. A camera that stops mid-capture, and the metrics widget, were real bugs
  this turned up.

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
