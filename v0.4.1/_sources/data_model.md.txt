(data-model)=
# The camera's data structure

A camera is one SpiriSynq object with three transports inside it. The YAML on this
page is the whole of that object, as `myhost/cameras/1/raw`, with its default values. The
structure and values come from a real camera's rehydrate. The `@` keys, RPCs and
comments are added by hand, because those don't appear in a rehydrate.

## How frames get to you

Every frame takes the same path, and each step runs only if something after it is
wanted:

```text
source -> resize -> widgets -> transport_raw -> transport_jpeg
                                             -> transport_video
```

- **Subscribing is what runs the camera.** With no subscribers on any transport, it
  isn't running and its source is closed. Nothing else switches it on.
- **`@` keys are how you choose a transport.** A wildcard never matches a key chunk
  starting with `@`, so the `cameras/1/raw/**` subscription every mirror holds receives
  settings, not frames. To get frames, subscribe to the exact key.
- **Everything is built on raw.** Resizing and widgets happen before raw, so every
  transport carries the same picture. For a feed without widgets, make a second
  camera.

## The camera

```yaml
# Every key below is prefixed with myhost/cameras/1/raw/
#
#   FIELD     Synced. One topic per field, YAML payload, sent when the value changes.
#             Reaches every mirror through its ** subscription, and is in the rehydrate.
#   READOUT   A FIELD the camera sets. Anything written to it gets overwritten.
#   VERBATIM  An @ key. Only explicit subscribers receive it. Opaque bytes, never in
#             a rehydrate, and sent only if someone would receive it.
#   RPC       A zenoh queryable: the caller does a get and receives a reply.

# --- Identity and placement: FIELD -----------------------------------------------
source_str: testimage://          # Writable; changing it retargets the camera.
source: !SourceInfo {url: testimage://, scheme: testimage, target: pm5544,
                     handler: TestImageSource, error: ''}
vendor: ''                        # These and max_supported_* come from opening the
model: ''                         # source once at start(), so an idle camera still
serial_number: ''                 # describes itself. The source then closes.
max_supported_width: null
max_supported_height: null
max_supported_framerate: null
intrinsics: null                  # The lens: see set_intrinsics below.
relative_to: !RootFrame 'unknown' # What the camera is mounted on.
mount: null                       # How it's mounted there.
pose: null                        # Where it is, and which way it looks.
overlay_widgets: !EventedDict {}  # {<widget topic>: {bindings: {alias: topic}, x: %, y: %}}
overlay_client_render: false      # true: widgets aren't drawn into the frames.

# --- State: READOUT --------------------------------------------------------------
running: false                    # true while anything wants raw, JPEG or video.
status: 'idle: no subscribers'    # running | stopped | waiting for source: <reason> |
                                  # capture failing: <reason>

# --- Pipeline: FIELD, live, applying to every transport --------------------------
max_width: 1920                   # The only size settings.
max_height: 1080
max_framerate: 30                 # The only frame rate setting.
exif_enabled: true                # false: frames carry no tags, on any transport.

# --- Raw -------------------------------------------------------------------------
transport_raw: !TransportRaw      # FIELD: empty, because raw has no settings.
  "@data":                        # VERBATIM, BACKGROUND priority, dropped when congested.
    # Sent:      Each capture tick with a new frame. An identical frame isn't resent.
    # Subscribe: cam.transport_raw_subscribe(), or a spirisynq+raw:// source.
    # Python:    .data (a read-only RawFrame), .data_changed (signal).
    payload: <height * width * 3 bytes, bgr24, tightly packed, top row first>
    attachment: {width: 1440, height: 1080, format: bgr24, tags: {timestamp: "..."}}
  request_keyframe:               # RPC: empty reply; the current frame is resent on @data.

# --- JPEG: encoded from raw, only if wanted --------------------------------------
transport_jpeg: !TransportJpeg
  quality: 80                     # FIELD, live.
  mimetype: image/jpeg            # FIELD, live.
  "@data":                        # VERBATIM, BACKGROUND priority, dropped when congested.
    # Subscribe: cam.transport_jpeg_subscribe(), or a spirisynq:// or spirisynq+jpeg:// source.
    # Python:    .data (bytes), .data_changed (signal).
    payload: <JPEG bytes, tags in the EXIF>   # zenoh encoding: image/jpeg
  request_keyframe:               # RPC: as for raw.

# --- Video: the camera's one encode, from raw, only if wanted --------------------
transport_video: !TransportVideo
  # Settings: FIELD, live.
  codec: h264                     # The only one.
  preset: veryfast                # x264's; default from SPIRICAMERA_H264_PRESET.
  keyframe_interval: 2.0          # Seconds; bounds recovery. 0: only when asked for.
  keyframe_min_interval: 0.25     # Coalesces requests, so there are no keyframe storms.

  # What the encoder is doing now: READOUT.
  target_bitrate: 0               # For the worst subscriber.
  encoded_fps: 0.0                # max_framerate, lower only when rate control says so.
  encoded_width: 0                # The camera's size, lower only if height_caps says so.
  encoded_height: 0

  rate_control: !RateControl      # FIELD, live: how the encode suits the worst subscriber.
    start_bitrate: 1000000        # A new subscriber's estimate; the target with no reports.
    min_bitrate: 500000           # Below this, frame rate drops instead.
    max_bitrate: 3000000
    min_fps: 2.0                  # The most frame rate can drop to.
    loss_high: 0.1                # Reported loss above this cuts the estimate.
    loss_low: 0.02                # Below this, the estimate grows.
    growth: 1.08                  # Per clean report.
    stall_timeout: 3.0            # Seconds silent, then the estimate halves, repeatedly.
    subscriber_timeout: 10.0      # Seconds silent, then it stops counting.
    bitrate_restart_fraction: 0.1 # How far the target must move to be worth applying.
    bitrate_restart_interval: 10.0  # Seconds without a keyframe before it gets its own.
    height_caps:                  # Smaller video at lower bitrates. Each row is
      - [1500000, null]           # [minimum target, height cap]; the first row the
      - [800000, 720]             # target reaches applies. null: no cap. [] never
      - [0, 480]                  # shrinks. Raw and JPEG stay full size.

  "@data_keyframe":               # VERBATIM, DATA_HIGH priority, dropped when congested.
    # Sent:      Every keyframe_interval, on request, and on an encoder restart.
    # Subscribe: cam.transport_video_subscribe(keyframes_only=True) for only this key.
    payload: <Annex B H.264, SPS and PPS in-band, decodable alone>   # zenoh encoding: video/h264
    attachment: {sequence: 1020, keyframe_sequence: 1020, pts: 91800000, codec: h264,
                 width: 1280, height: 720, tags: {timestamp: "..."}}

  "@data_interframe":             # VERBATIM, DATA_LOW priority, dropped when congested.
    # Sent:      Every other frame. Not even encoded if nobody wants it.
    # Subscribe: cam.transport_video_subscribe(), or a spirisynq+video:// source.
    payload: <Annex B H.264, useless without its chain back to the keyframe>
    attachment: {sequence: 1021, keyframe_sequence: 1020, pts: 91803000, codec: h264,
                 width: 1280, height: 720, tags: {timestamp: "..."}}

  request_keyframe:               # RPC: empty reply. Does the job of RTCP PLI and FIR.
  report:                         # RPC: empty reply. Does the job of RTCP RR and REMB.
    # Payload: {subscriber: <id>, fraction_lost: 0.03, max_bitrate: null}, about once a second.
    #      or: {subscriber: <id>, left: true}

  # Python, on the camera or a mirror:
  #   .packet_received                  Signal: each VideoPacket, in decodable order.
  #   cam.transport_video_subscribe() / _unsubscribe()
  #   cam.transport_video_request_keyframe()
  # Python, in the camera's own process, for an endpoint such as RTSP:
  #   cam.transport_video_hold() / _release()
  #   cam.transport_video_report(id, fraction_lost=, max_bitrate=) / _forget(id)

# --- RPCs on the camera: authoritative only --------------------------------------
sr_rehydrate:                     # Everything above except @ keys and RPCs.
sr_rehydrate_client:
sr_metadata/<tag>:                # Topic, classes, authoritive_node.
sr_type_schema/<tag>:             # JSON schema.
set_intrinsics:                   # origin, width, height, fx, fy, cx, cy, hfov, vfov.
clear_intrinsics:                 # origin.

# --- Never on the wire -----------------------------------------------------------
# received_width, received_height, received_ratio, received_framerate, exif_tags,
# exif_timestamp: each node works these out from the frames it receives. A mirror
# has none until it subscribes to something.
#
# data_qos, data_keyframe_qos, data_interframe_qos: the priorities above. Local to the
# sender, as SpiriSynq's <field>_qos is, and set in code on the class or instance.
```

The priorities are defaults, meant to be tuned on the real link:

```python
import zenoh
from SpiriSynq.qos import SynqQoS

cam.transport_video.data_interframe_qos = SynqQoS(
    priority=zenoh.Priority.DATA,
    congestion_control=zenoh.CongestionControl.DROP,
)
```

## What gets sent, and when

### A raw or JPEG subscriber

```yaml
- mirror: cam.transport_jpeg_subscribe()
  wire:
    - declare subscriber on transport_jpeg/@data
    - get transport_jpeg/request_keyframe       # In case the frame isn't changing.
  camera, within 0.1 s:
    - opens the source, publishes running and status
    - captures, resizes, draws widgets
    - encodes JPEG, because something wants it; video stays unencoded
    - put transport_jpeg/@data, for each changed frame
  mirror:
    - transport_jpeg.data updates, and data_changed fires
    - received_width, exif_tags and the rest are read out of the frame

- mirror: cam.transport_jpeg_unsubscribe()
  wire:
    - undeclare subscriber
  camera:
    - stops encoding JPEG
    - closes the source, if nobody wants anything else
```

### A video subscriber

```yaml
- mirror: cam.transport_video_subscribe()
  wire:
    - declare subscribers on @data_interframe and @data_keyframe
    - get transport_video/request_keyframe
  camera, within 0.1 s:
    - opens the source, publishes running and status
    - encoder sends a keyframe, then interframes
  mirror:
    - discards interframes until a keyframe arrives
    - packet_received fires for each decodable packet
    - get transport_video/report, about once a second

- on a gap, when sequence jumps:
    mirror:
      - stops emitting, and discards until a keyframe arrives
      - get transport_video/request_keyframe, at most every 0.5 s
      - counts the gap in its next report
    camera:
      - next frame is a keyframe, within keyframe_min_interval, for every subscriber

- mirror: cam.transport_video_unsubscribe()
  wire:
    - undeclare subscribers
    - get transport_video/report {subscriber: <id>, left: true}
  camera:
    - takes it out of rate control straight away
    - stops encoding, if nobody else wants video
```

### Rate control

Each subscriber has its own estimate of what its link can carry, whichever way its
reports arrive. The encoder takes the worst of them.

```yaml
reports:
  zenoh subscriber:                     # Counts sequence gaps itself.
    get transport_video/report: {subscriber: <id>, fraction_lost: 0.03}
  RTSP viewer on /<camera>:             # SpiriCamera.rtsp's LossMeter, every second.
    NACKs, receiver reports, or silence: cam.transport_video_report("rtsp-<id>", fraction_lost=<loss>)
    PLI or FIR:      cam.transport_video_request_keyframe()
  RTSP reader on /<camera>/relay: nothing  # A relay on the same host has no say.
  keyframes-only subscriber: nothing    # It can't see loss, so it has no say.
  subscriber that never reports: nothing

each report moves that subscriber's estimate:
  fraction_lost > loss_high:     estimate *= 1 - fraction_lost / 2
  fraction_lost < loss_low:      estimate *= growth
  otherwise:                     unchanged
  max_bitrate:                   a ceiling on the estimate
  silent for stall_timeout:      estimate /= 2, repeatedly
  silent for subscriber_timeout: forgotten

the encoder takes the lowest of everyone's:
  target_bitrate: estimate, held between min_bitrate and max_bitrate
                  # Applied at the next keyframe, or after bitrate_restart_interval,
                  # since changing it needs a new encoder, and so a keyframe.
  encoded_fps:    max_framerate, scaled down once the estimate is below min_bitrate
                  # Applied immediately. Never below min_fps.
  encoded_height: the height_caps entry for the target
                  # Applied immediately, with a keyframe.
  with no reports at all: start_bitrate, at max_framerate, with no height cap.
```

The height cap is there because a low bitrate spread over a large frame gives each
pixel too few bits, and the picture breaks up into blocks. Fewer pixels look better
on the same link. With the defaults, a target of 1.5 Mbit/s or more has no cap, 0.8
up to 1.5 Mbit/s caps video at 720 pixels high, and anything lower caps it at 480.
A frame already shorter than its cap isn't touched, and a shrunk frame keeps its
aspect ratio, so 1920x1080 capped at 720 is encoded at 1280x720.

### A chain of cameras

A camera with a `spirisynq://` source subscribes upstream only while it's running
itself, so demand travels up the chain.

`SpiriCamera run` is one: its `hud` and `preview` cameras both relay `raw`.

```yaml
myhost/cameras/1/hud:                   # source: spirisynq+raw://./cameras/1/raw
  viewer subscribes to hud/transport_jpeg/@data:
    - hud starts running, draws its own widgets, encodes its own JPEG
    - hud's source subscribes to raw/transport_raw/@data
myhost/cameras/1/raw:
  - raw starts running; its JPEG and video stay unencoded
viewer leaves:
  - hud stops, and its source unsubscribes
  - raw stops, and closes its source
```

| Source scheme | Upstream transport | Cost here |
| --- | --- | --- |
| `spirisynq://`, `spirisynq+jpeg://` | JPEG | A JPEG decode per frame, and generation loss |
| `spirisynq+raw://` | Raw | No decode or loss, but large on the network |
| `spirisynq+video://` | Video | An H.264 decode, and low network use; reports loss upstream |

## Related pages

- {ref}`cpp-integration` uses this structure from raw zenoh, with no Python.
- {ref}`rtsp` covers how RTSP viewers drive rate control.
- {ref}`mediamtx` covers translating the camera's RTSP stream for other readers.
- {py:mod}`SpiriCamera.transports` and {py:mod}`SpiriCamera.video` are the code.
