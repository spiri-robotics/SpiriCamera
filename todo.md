Don't super trust latency count right now.
Max width/height are not respected, user can not reduce resolution
Not sure how exif tags are working. The use case for this is software adding metadata to the stream. We don't seem to be able to remove custom tags? Multipl eproviders don't seem to be able to manage tags?


Follow-up, on hold for now: proper STANAG 4609 interop means encoding our tags as MISB ST 0601 KLV and giving SpiriCamera an RTSP/SRT output with a synchronous KLV track (RFC 6597, SMPTE336M) -- that's what MediaMTX actually carries end-to-end, unrelated to WHEP/WebRTC. Have a design sketch of the KLV dataclass (tag/codec metadata per field) but haven't built it.

What one of these (`UasDatalinkSet`, pre-KLV-encoding, before `to_klv()` packs it into TLV bytes) would look like as YAML -- field names/units match ST 0601 Table 1 verbatim, `None`/absent means "no source for this yet", not zero:

```yaml
# One UasDatalinkSet instance, human-readable form.
# Tag 2, mandatory -- everything else may be omitted if we have no source for it.
timestamp: 1798675200.123456        # Tag 2, unix seconds -> encoded as whole microseconds

mission_id: spiricamera_dev_video0  # Tag 3, free text -- our SpiriSynq topic today
platform_tail_number: null          # Tag 4 -- no platform identity concept yet

# Tags 5-7: none of these exist without an IMU. Omitted from an encoded
# packet entirely, not sent as 0.0.
platform_heading_deg: null
platform_pitch_deg: null
platform_roll_deg: null

sensor_name: "SpiriCamera testimage"  # Tag 11, free text

# Tags 13-15: none of these exist without GPS. Same story as above.
sensor_latitude_deg: null
sensor_longitude_deg: null
sensor_altitude_m: null

# Tags 23-25: terrain position at frame center -- also GPS-derived, also null today.
frame_center_latitude_deg: null
frame_center_longitude_deg: null
frame_center_elevation_m: null
```

Filled in with an actual GPS/IMU source, the same instance would look like:

```yaml
timestamp: 1798675200.123456
mission_id: spiricamera_dev_video0
platform_tail_number: "AF008"
platform_heading_deg: 123.45
platform_pitch_deg: -5.2
platform_roll_deg: 1.1
sensor_name: "EO Nose"
sensor_latitude_deg: 45.5017
sensor_longitude_deg: -73.5673
sensor_altitude_m: 1200.0
frame_center_latitude_deg: 45.5020
frame_center_longitude_deg: -73.5680
frame_center_elevation_m: 40.0
```

---

Chunked progressive MJPEG. Figure out the chunk size to have a useful image, send it as hard real time with zenoh's priority. the tail end as InteractiveLow or DataLow?. Still use the same basic channel, but have some ability merge segments. Probably need a segment number too, a wrap-around byte. We can also set a target bitrate, anything beyond that gets DataLow? Thinking about use case, it's easy to put basic jpeg at interactivelow, we prioritize only like mavlink streams more. Telemetry data in general. Need to figure out how we should prioritize stuff. want explicit chunk boundaries so the receiver can decode at any cut. I think we want InteractiveLow for enough to see what you're getting, DataLow to ge tthe jpeg up to a target bitrate, and Background for anything over that. One byte for frame ID is probably fine it would roll over every few seconds at 30fps. Background chunks probably need to be tagged with a timestamp...