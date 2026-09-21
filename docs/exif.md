# Frame Metadata (EXIF)

Each captured frame is a JPEG with its own metadata baked into its EXIF:
when it was taken, what took it, and anything you care to add — a GPS
fix, a mission name, a battery level. This page covers reading that
metadata back, adding your own, and sharing the job between more than
one piece of software. For the underlying module (`embed`, `extract`,
`is_jpeg`, `image_size`) used directly on raw JPEG bytes, see
{doc}`api/SpiriCamera.exif`.

## Reading Tags

```python
cam = Camera("/dev/video0")
cam.exif_update(mission="probe-1")
cam.start()

cam.exif_tags  # {'timestamp': '1789...', 'make': ..., 'mission': 'probe-1'}
cam.exif_timestamp  # Unix timestamp of the frame currently in cam.image
```

`exif_tags` and `exif_timestamp` are read back *out of* `cam.image`, and
so are `received_width`, `received_height` and `received_ratio`. None of
them is sent over SpiriSynq — they are properties, not synced fields, so
there is nothing there *to* send. That is on purpose: SpiriSynq publishes
each field independently and promises nothing about two of them arriving
together, so a timestamp sent beside an image is a timestamp a peer can
read against the wrong image. Travelling inside the JPEG, they cannot
come apart from it — a peer that receives a frame fills them in for
itself, exactly:

```python
mirror.image = frame  # the only thing that crossed the network
mirror.exif_timestamp  # when the far end took it
mirror.received_width  # what it actually sent
```

## Multiple Tag Providers

More than one piece of software may want to tag the same camera's
frames — a logger, a telemetry service, a human poking at the debug UI.
`exif_set_tags` gives each one its own namespace, so setting one never
touches another's:

```python
cam.exif_set_tags("telemetry", {"battery": "88"})
cam.exif_set_tags("logger", {"build": "42"})

cam.exif_clear_tags("telemetry")  # only "battery" goes away
```

Calling `exif_set_tags` again for the same provider *replaces* that
provider's tags rather than merging into them — drop a key by leaving it
out next time. A value of `""` is kept rather than dropped, which is how
a provider suppresses one of the built-in tags (say, omitting `source`
from a frame headed somewhere public). Providers merge over the built-ins
in sorted-name order, so the result does not depend on which one
registered first; two providers naming the *same* tag still resolve in
that order, which is worth avoiding with a distinguishing prefix rather
than relying on.

`exif_update(**tags)` is a shorthand for the common case of one caller
adding a tag or two with no provider name of its own — it merges into a
shared default bucket, and a value of `""` there removes just that tag:

```python
cam.exif_update(mission="probe-1")
cam.exif_update(mission="")  # removes it; other tags untouched
```

Two *different* callers both using `exif_update` still share that one
bucket and can still overwrite each other; once that matters, give each
one a provider name and use `exif_set_tags` instead.

To attach something the camera itself can compute, override the method
that builds the built-in tags — this still gets whatever providers have
set merged on top:

```python
class SurveyCamera(Camera):
    def exif_tags_for_frame(self, frame):
        tags = super().exif_tags_for_frame(frame)
        tags["gps"] = f"{self.fix.latitude},{self.fix.longitude}"
        return tags
```

Tag names are free-form; the whole set round-trips through the EXIF
`UserComment` field. Names that have a real EXIF equivalent — `make`,
`model`, `serial_number`, `software`, `description`, `datetime` — are
also written to that tag, so ordinary image tools show something
sensible.

Because a tag records a capture time, two captures of an unchanging
scene produce different bytes. Set `exif_enabled = False` for
byte-identical frames, which psygnal then suppresses rather than
republishing.

## Example: Tagging Frames With GPS

A GPS fix is exactly the kind of thing that changes on its own schedule,
independent of the frame rate, so the natural way to attach it is
`exif_update` from whatever callback your GPS source already gives you
— not something computed inside `Camera` itself, and not something worth
a subclass for:

```python
cam = Camera("/dev/video0")
cam.start()


def on_gps_fix(lat: float, lon: float) -> None:
    cam.exif_update(gps_lat=f"{lat:.6f}", gps_lon=f"{lon:.6f}")


gps.events.fix.connect(on_gps_fix)  # or however your GPS source notifies you
```

Every frame captured after that carries the most recent fix — there is
no need to re-call `exif_update` per frame, since `exif_tags_for_frame`
re-merges whatever the default bucket currently holds into *every*
frame it builds. A fix that goes stale (GPS lost) should be cleared the
same way, with an empty string, rather than left showing a last-known
position as if it were current:

```python
cam.exif_update(gps_lat="", gps_lon="")  # drop both; other tags untouched
```

If your GPS source lives inside the `Camera` subclass itself — an
onboard receiver polled on the same cadence as capture, say — override
`exif_tags_for_frame` instead, so the fix is read fresh for the frame
actually being encoded rather than from whatever `exif_update` last
pushed:

```python
class SurveyCamera(Camera):
    def exif_tags_for_frame(self, frame):
        tags = super().exif_tags_for_frame(frame)
        fix = self.gps.read()
        if fix is not None:
            tags["gps_lat"] = f"{fix.latitude:.6f}"
            tags["gps_lon"] = f"{fix.longitude:.6f}"
        return tags
```

Either way, `gps_lat`/`gps_lon` are free-form tag names, not a real EXIF
GPS IFD entry — they round-trip through the `UserComment` JSON exactly
like any other tag, but a tool that reads a JPEG's *native* GPS fields
(only `make`, `model`, `serial_number`, `software`, `description` and
`datetime` get written there; see `STANDARD_TAGS` in
{py:mod}`SpiriCamera.exif`) will not see them. Read them back with
`cam.exif_tags["gps_lat"]`, or `exif.extract(data)["gps_lat"]` on raw
bytes via {py:mod}`SpiriCamera.exif`.

## Reading Tags From Raw JPEG Bytes

`cam.exif_tags` is a convenience for when you already have a `Camera`.
If all you have is JPEG bytes — a frame saved to disk, one pulled off an
HTTP response, `mirror.image` read before you bothered to build a
mirror at all — {py:mod}`SpiriCamera.exif` reads and writes the same
tags directly, with no `Camera` involved:

```python
from SpiriCamera import exif

data = open("frame.jpg", "rb").read()

exif.is_jpeg(data)  # True — cheap check, just the magic bytes
exif.extract(data)  # {'timestamp': '1789...', 'make': ..., 'mission': 'probe-1'}
exif.image_size(data)  # (1920, 1080) — read from the JPEG header, not decoded
```

`extract` never raises: bytes with no tags, damaged EXIF, or a format
that carries none all read back as `{}`, so it is safe to call on
whatever you happen to have. It reads the same `UserComment` JSON blob
`Camera` writes, so an arbitrary tag name round-trips exactly; given a
JPEG this package did not tag at all, it falls back to whatever
standard EXIF fields (`Make`, `Model`, `DateTime`, ...) the file
happens to carry.

`embed` is the write side, and is what `Camera` calls internally on
every captured frame:

```python
tagged = exif.embed(data, {"mission": "probe-1", "operator": "alex"})
```

Tagging is a best effort layered on top of whatever encoding already
happened: `embed` returns non-JPEG data unchanged rather than raising,
and only raises `exif.ExifError` for a JPEG it could not tag. Calling
it again replaces the EXIF wholesale rather than accumulating segments,
so re-tagging a frame — or a mirror re-deriving its own tags from
`image` — never leaves stale data behind.
