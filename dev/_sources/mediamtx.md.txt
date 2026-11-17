(mediamtx)=
# Serving a camera through MediaMTX

[MediaMTX](https://github.com/bluenviron/mediamtx) translates a camera's
{ref}`RTSP <rtsp>` stream for readers that need another protocol, such as STANAG 4609
consumers reading MPEG-TS over SRT, and browsers using WebRTC.

Run it on the same host as the camera, reading a camera's relay mount, here `hud`'s:

```yaml
paths:
  camera1:
    source: rtsp://127.0.0.1:8554/hud/relay
    rtspTransport: tcp
    sourceOnDemand: yes
    sourceOnDemandStartTimeout: 20s
    sourceOnDemandCloseAfter: 10s
```

| Setting | Why |
| --- | --- |
| `rtspTransport: tcp` | A relay mount is TCP only, which also avoids any UDP in or out of a container. |
| `sourceOnDemand: yes` | MediaMTX pulls only while it has a reader. Without this it pulls all the time, so the camera never idles. |
| `sourceOnDemandStartTimeout: 20s` | An idle camera has to open its source before the first reader sees a frame. |
| `sourceOnDemandCloseAfter: 10s` | How long the camera keeps running after the last reader leaves. Raise it to make quick reconnects cheaper, or lower it to idle sooner. |

The behaviour on this page was measured with MediaMTX 1.21.3.

## Read STANAG 4609 output over SRT

With SRT enabled in `mediamtx.yml`, read the path:

```text
srt://mediamtx.example:8890?streamid=read:camera1
```

The stream is MPEG-TS with H.264 and a synchronous KLV track (stream type `0x15`). Each
KLV packet's PTS was within a tenth of a millisecond of its frame's, over all the
packets checked. The first few packets a reader receives can come before its first
video frame, because a reader starts at a keyframe.

:::{warning}
This hasn't been tested with a STANAG 4609 client or analyzer. MediaMTX only carries
the KLV, so what's in it is up to the camera, and {ref}`that's incomplete so far <rtsp>`.
:::

MediaMTX's WebRTC output also sends KLV, on a data channel labelled `KLV`, but with no
timestamps and no guarantee of order, so it can't be matched to frames exactly.

## Rate control

Readers of MediaMTX have no effect on the camera's bitrate: `/hud/relay` has no say, and
MediaMTX doesn't pass its readers' RTCP upstream. So a reader across a constrained link
is only protected if a viewer on `/hud` is reporting on that same link, or if
`rate_control.max_bitrate` is set to what the link can carry:

```python
cam.transport_video.rate_control.max_bitrate = 1_500_000
```

SRT repairs its own loss, up to its latency setting.

## Check that it's working

MediaMTX logs `2 tracks (H264, KLV)` when the path comes online. With no readers, the
camera's `status` should read `idle: no subscribers`. If it doesn't, look for a path
without `sourceOnDemand`, or for another subscriber.
