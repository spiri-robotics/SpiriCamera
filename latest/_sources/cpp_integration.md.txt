(cpp-integration)=
# Using a camera from a C++ app

A camera's frames are plain zenoh samples, so a C++ app can read them without
SpiriSynq or Python. This guide uses [zenoh-cpp](https://github.com/eclipse-zenoh/zenoh-cpp),
which wraps the Rust implementation through zenoh-c. A Rust app can use the same
keys and formats through the `zenoh` crate.

The examples here were compiled against zenoh-cpp 1.9.0 and run against a camera
using eclipse-zenoh 1.10.1 in Python. The video example's output was then decoded
to check it.

{ref}`data-model` has the camera's whole structure as annotated YAML, and this
guide is the same thing from C++.

## Find the camera

Every key below starts with the camera's full path, written here as `<camera>`.
Read it from `cam.synq_absolute_path`. Your session has to reach the same zenoh
network as the camera, usually by connecting to the zenoh router:

```json5
{ mode: "peer", connect: { endpoints: ["tcp/zenoh-router:7447"] } }
```

## Choose a transport

Subscribing is what makes a camera run. With no subscribers its source is closed,
so subscribe only while you need frames, and to one transport only.

| Key under `<camera>/` | Payload | Attachment (JSON) |
| --- | --- | --- |
| `transport_raw/@data` | BGR pixels in one contiguous block, top row first | `width`, `height`, `format`, `tags` |
| `transport_jpeg/@data` | One JPEG, with tags in its EXIF | None |
| `transport_video/@data_keyframe` | Annex B H.264, with SPS and PPS | See [the video section](#read-video) |
| `transport_video/@data_interframe` | Annex B H.264 | See [the video section](#read-video) |

Subscribe to these exact keys. A key chunk starting with `@` is never matched by a
wildcard, so `<camera>/**` receives no frames.

Each transport also has a `request_keyframe` queryable. Query it with no payload
and the current frame is sent again on the data key. The reply is empty.

| Priority | Keys | Under congestion |
| --- | --- | --- |
| `DATA_HIGH` | Video keyframes | Dropped |
| `DATA_LOW` | Video interframes | Dropped |
| `BACKGROUND` | Raw and JPEG | Dropped |

## Read raw frames

Raw is the simplest transport, and the right one when your app is on the same host
or a fast network. It carries about 139 MB/s at 1440x1080 and 30 frames per second,
so don't send it over a modem.

```{literalinclude} ../examples/cpp/raw_view.cpp
:language: cpp
```

(read-video)=
## Read video

Video suits any constrained link, and takes three things beyond subscribing:

1. Subscribe to both keys, and keep packets in order yourself.
2. Ask for a keyframe when you lose your place.
3. Report your loss once a second.

Each packet's attachment has these fields:

| Field | Meaning |
| --- | --- |
| `sequence` | Counts up by one per packet, across both keys. |
| `keyframe_sequence` | The `sequence` of the keyframe this packet depends on. |
| `pts` | Presentation time on a 90 kHz clock. |
| `width`, `height` | The encoded size, which rate control can change at a keyframe. |
| `codec` | `h264`. |
| `tags` | The frame's tags. |

The two keys have different priorities, so a keyframe can overtake the interframes
before it. Apply these rules to every packet:

- Until a keyframe arrives, discard everything else.
- Discard an interframe older than the current keyframe.
- An interframe that isn't next in sequence is a gap. Stop decoding, and query
  `request_keyframe` no more than every 0.5 seconds until a keyframe arrives.

```{literalinclude} ../examples/cpp/video_view.cpp
:language: cpp
```

### Report to rate control

The camera has one encode, and it suits the worst subscriber that reports. Query
`<camera>/transport_video/report` once a second, with a JSON payload:

```json
{"subscriber": "cpp-viewer", "fraction_lost": 0.02}
```

| Field | Meaning |
| --- | --- |
| `subscriber` | Your ID. Keep it stable, and unique among this camera's subscribers. |
| `fraction_lost` | Packets lost since your last report, from 0 to 1. |
| `max_bitrate` | Optional. A ceiling you already know, in bit/s. |
| `left` | Send `true` when leaving, in place of the loss fields. |

Reporting is a commitment. A subscriber that never reports has no influence on the
encode. Once you have reported, 3 seconds of silence (`stall_timeout`) starts
halving your estimate, and everyone else's video gets worse with it. After 10
seconds (`subscriber_timeout`) you stop counting.

### Take keyframes only

To sample a camera occasionally, subscribe only to `@data_keyframe`. You get one
frame every `keyframe_interval` seconds, with no interframes encoded for you if
nobody else wants them. Every keyframe decodes on its own. Sequence numbers will
skip, which isn't loss, so don't report.

While anyone else is watching, a camera with `intra_refresh` turned on sends
keyframes only when one is asked for, so you may wait longer. Call
`request_keyframe` for one now.

## Build

zenoh-cpp is header-only and needs the zenoh-c library:

```bash
g++ -std=c++17 -DZENOHCXX_ZENOHC \
    -I"$ZENOH_CPP/include" -I"$ZENOH_C/include" -I"$NLOHMANN_JSON/include" \
    video_view.cpp -o video_view \
    -L"$ZENOH_C/lib" -lzenohc -pthread
```

The examples use [nlohmann/json](https://github.com/nlohmann/json) for attachments.
Any JSON parser will do.

## Draw widgets on the camera

You don't need a renderer in your app. Widgets are drawn on the camera after the
resize and before any transport, so your app receives them already in the pixels.

:::{note}
This section is an outline of the specification. Publishing a widget from C++
hasn't been built or tested.
:::

A widget is a SpiriSynq object, `HudWidget`, with these fields:

| Field | Meaning |
| --- | --- |
| `svg_template` | MiniJinja source that produces an SVG. |
| `anchor` | `top_left` through `bottom_right`, or `custom`. |
| `custom_anchor_x`, `custom_anchor_y` | Percent of the frame, used with `custom`. |
| `z_index` | Paint order. Higher paints on top. |

A template can read three namespaces:

- `exif`: the frame's tags.
- `frame`: its actual width, height and frame rate.
- `objects`: fields of any SpiriSynq object, declared in the template itself.

```text
{# object: battery = mu-1/battery_monitor #}
<svg width="200" height="40">
  <text x="4" y="24" fill="white">{{ objects.battery.voltage }} V</text>
</svg>
```

A camera draws the widgets named in its `overlay_widgets` field. {ref}`The overlay
layer <the-overlay-layer>` covers the details, and {ref}`world-overlays` covers
pinning a widget to a place in the world.

Being a SpiriSynq object means answering SpiriSynq's metadata and rehydrate queries
as well as publishing YAML, so implementing one in C++ is real work. These two
shortcuts avoid it:

- **Use a small Python process.** Let it own the `HudWidget` and a SpiriSynq object
  holding your data, and have your C++ app send it updates over zenoh.
- **Keep a clean feed.** Widgets end up in every transport of the camera that draws
  them. For a feed with and without, make a second camera whose source is
  `spirisynq+raw://<camera>`, and draw on that one.
