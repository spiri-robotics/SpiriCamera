Don't super trust latency count right now.
Max width/height are not respected, user can not reduce resolution
Not sure how exif tags are working. The use case for this is software adding metadata to the stream. We don't seem to be able to remove custom tags? Multipl eproviders don't seem to be able to manage tags?


Bigger project, webrtc and SHRED. Need to support webrtc receiving and sending, keep our tagged metadata in the stream proeprly. Session initation via a zenoh RPC call and the ability to expose a topic over http for SHRED.

Done: WHEP (not SHRED) egress/ingest via SpiriCamera.webrtc + SpiriCamera.whep + sources/webrtc.py, `webrtc_offer` zenoh RPC, `SpiriCamera whep-serve` CLI. Tags travel over a `spiricamera-tags` data channel, but only when the *viewer's* own offer opens it -- a single-shot WHEP answer can't add one the offer didn't have. MediaMTX can pull our WHEP stream directly (`source: whep://...` in mediamtx.yml, see docs/architecture.md) but drops the tags channel entirely -- it has no data channel support at all right now.

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


#Overlay system. Maybe a seperate project? 

It's a bit jumped together, rendering widgets, showing widgets, etc. Think it should probably be a mixin class we can add to a camera to enable overlay support.

The overlay system, oh god the overlay system. An overlay is an SVG. It can be anchored to top_right,  top_middle, top_left. center_left, center_middle, center_right. Bottom_etc. (how do we cover everything? Like for an ML bounding box?) ALso anchored to coordinates (in percentage, not pixels). That's a property of the hudWidget syncableobject.


HUDwidgets get zenoh topics injected into them as style elements.

The SVG gets templated by MiniJinja, and has access to raw zenoh topics and image exif data.

The SVG can be baked directly into a camera image with thorvg-python.

 or rendered in the browser itself. (note, no virtual DoM, this is going to be expensive for the browser to do. I'd like to support 120fps)

 Widget would look something likde

 class HudWidget:
     svg_template: str
     anchor: left,right,middle,cutom
     custom_anchor_x: 80 (percent)
     custom_anchor_y: 50 (percent)

A camera can support overlays as a subscriber, or CSV ofs subcribers. "overlays/**" (default) would grab call overlays for example. We don't nessearily need to instantiate a HudWidget object on the client side to do this, just build a generic attribute tree. We'll need to figure how topics work, can we statically analyse it to figure out what topics we need to cache or what?

>MiniJinja has exactly the API you need: Template::undeclared_variables(), which "returns a set of all undeclared variables in the template... since this runs a static analysis

Yes on static analysis.

---
