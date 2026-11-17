(rtsp)=
# Serving a camera over RTSP

`SpiriCamera run` serves its three cameras over RTSP: `raw`, `hud` and `preview`.
Every reader of a camera shares that camera's one H.264 encode, and each frame carries
KLV metadata on a second track, a MISB ST 0601 local set.

Each camera has two mounts, here for `hud`:

| Mount | For | Transport | Lowers the bitrate? |
| --- | --- | --- | --- |
| `rtsp://robot:8554/hud` | A viewer across a link that needs protecting, such as QGroundControl | UDP only | Yes |
| `rtsp://robot:8554/hud/relay` | A relay on the same host, such as {ref}`MediaMTX <mediamtx>` | TCP only | No |

A camera only runs while a session is playing it, so it idles when nobody is watching.

:::{warning}
Everything on this page was measured on one machine, with loss injected at the
server. It hasn't been tested on real modems, or through a published docker port.
:::

## Configure the server

```bash
export SPIRICAMERA_RTSP_PORT=8554      # TCP, for RTSP. 0 serves none.
export SPIRICAMERA_RTSP_UDP_PORT=8000  # The first of four UDP ports.
SpiriCamera run
```

The four UDP ports are, in order, the RTP and RTCP ports for video, then the same for
metadata. A viewer must be able to reach them, and the server must be able to reach
the UDP ports the viewer asks for.

## View the camera in QGroundControl

In QGroundControl's video settings, choose **RTSP Video Stream** and set the URL to
`rtsp://robot:8554/raw`. QGroundControl draws a HUD of its own, so it doesn't need
`hud`'s.

This was tested with QGroundControl 5.1.5. QGroundControl plays the video and ignores
the KLV track. It sometimes fails to link the stream and reconnects a few times in
its first seconds, which takes about a second each time.

A viewer's mount refuses TCP, because TCP hides loss from rate control. If UDP
can't get through at all, QGroundControl keeps retrying, waiting up to 30 seconds
between attempts, and shows no video.

## How viewers drive rate control

A viewer on a camera's own mount, such as `/raw`, reports through RTCP, and {py:class}`~SpiriCamera.rtsp.LossMeter`
turns that into reports to rate control each second. It has three sources:

- **NACKs:** a viewer asks for each packet it's missing, and the server sends it again.
  The fraction of packets asked for is the loss. Retransmission can repair the picture,
  but this loss still lowers the bitrate, because retransmitting adds load to the link.
- **Receiver reports:** these cover a viewer on a clean link, or one that doesn't send
  NACKs. A clean report counts once for each second since the last sign of trouble,
  because GStreamer only sends one every 3 to 7.5 seconds.
- **Silence:** after 8 seconds with no RTCP at all, the viewer counts as having lost
  everything, until it's heard from again.

A viewer that has never sent RTCP has no say. A `/relay` mount never has a say, so
readers behind a relay don't affect the bitrate.

### What was measured

QGroundControl 5.1.5 at 640x480 and 30 fps, with a `max_bitrate` of 3 Mbit/s:

| Phase | Length | Target at the end | Frame rate at the end |
| --- | --- | --- | --- |
| No loss | 20 s | 1.71 Mbit/s | 30 fps |
| 15% random loss | 20 s | 1.00 Mbit/s | 30 fps |
| 30% random loss | 20 s | 0.60 Mbit/s | 30 fps |
| Blackout | 8 s | 0.50 Mbit/s | 29 fps |
| No loss, after 25 s | 25 s | 0.97 Mbit/s | 30 fps |

The session survived the blackout. The picture itself wasn't assessed, because
QGroundControl ran without a display.

## KLV metadata

The metadata track is RFC 6597 KLV, one local set per frame, on the same RTP timestamp
as its frame. Each set holds the frame's capture time (tag 2), the ST 0601 version
(tag 65) and the checksum (tag 1).

:::{warning}
The local set holds no position, pointing or security metadata yet, so it isn't enough
for STANAG 4609 consumers.
:::

{ref}`mediamtx` covers carrying the track on into MPEG-TS.

## Limitations

- **H.264 only.** It's the only codec the camera publishes.
- **One encode per camera, shared.** One poor viewer on `/hud` lowers the quality for
  every reader of `hud`, including those on `/hud/relay`. The other cameras have encodes
  of their own, so `raw` is unaffected.
- **Slow to notice an outage.** Silence takes 8 seconds to count, because reports can
  be that far apart on a clean link.
- **A session lasts as long as its RTSP connection.** Losing the TCP connection ends
  the session, even if the UDP streams still work.
