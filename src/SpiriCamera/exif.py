"""Frame metadata carried *inside* the encoded frame.

Anything that describes a particular frame has to travel with that
frame.  SpiriSynq publishes each field of a :py:class:`SyncableObject`
independently and makes no promise that two of them arrive together or
in order, so a tag published beside an image is a tag that can be read
against the wrong image.  Baking the tags into the image bytes removes
the question: there is one payload, and it either arrived or it did not.

The consequence for the camera domain is that tags are *written* on the
authoritative side and *read* on every side, including the authoritative
one.  Nothing measured off a frame is ever sent as its own field; see
the ``synq_skip_sync`` set on
:py:class:`~SpiriCamera.camera.CameraBase`.

Tags are free-form ``str`` to ``str``.  The whole mapping is serialised
as JSON into the EXIF ``UserComment`` field, which is what
:py:func:`extract` reads back, so an arbitrary key round-trips exactly.
Keys in :py:data:`STANDARD_TAGS` are *additionally* written to the real
EXIF tag of the same meaning, so that ordinary image tools see a sensible
Make and Model; :py:func:`extract` falls back to reading those when an
image has no ``UserComment``, which is what lets tags be recovered from a
JPEG this package did not write.
"""

from __future__ import annotations

import io
import json
import struct
from collections.abc import Mapping

import piexif

#: Tag holding the capture time as a Unix timestamp, in seconds.
#:
#: A wall clock rather than :py:func:`time.monotonic`, because a tagged
#: frame is meant to be read on a different node than the one that took
#: it and a monotonic reading means nothing once it leaves the process
#: that made it.  The cost is the usual one: it can step, and two nodes
#: only agree to the accuracy of whatever is keeping their clocks
#: together.
TIMESTAMP_TAG = "timestamp"

#: Tag holding the same instant as EXIF's ``YYYY:MM:DD HH:MM:SS`` text.
#:
#: Redundant with :py:data:`TIMESTAMP_TAG` and derived from it, so that
#: ordinary image tools see a date in the field they look in.  Whole
#: seconds and no timezone, which is all the EXIF tag holds; read
#: :py:data:`TIMESTAMP_TAG` for the real value.
DATETIME_TAG = "datetime"

#: Tags that also map onto a real EXIF tag, as ``(ifd, tag id)``.
#:
#: Written in both places; read back from here only when an image carries
#: no JSON ``UserComment`` to read instead.
STANDARD_TAGS: dict[str, tuple[str, int]] = {
    "make": ("0th", piexif.ImageIFD.Make),
    "model": ("0th", piexif.ImageIFD.Model),
    "software": ("0th", piexif.ImageIFD.Software),
    "description": ("0th", piexif.ImageIFD.ImageDescription),
    DATETIME_TAG: ("0th", piexif.ImageIFD.DateTime),
    "serial_number": ("Exif", piexif.ExifIFD.BodySerialNumber),
}

#: EXIF's 8-byte character-set prefix on ``UserComment``.
_ASCII_PREFIX = b"ASCII\x00\x00\x00"

_JPEG_MAGIC = b"\xff\xd8"

# SOFn markers carry the frame dimensions. 0xC4, 0xC8 and 0xCC sit in the
# same range but are DHT, JPG and DAC respectively.
_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


class ExifError(ValueError):
    """Raised when image data cannot be tagged."""


def is_jpeg(data: bytes) -> bool:
    """Whether image data is a JPEG, and so can carry EXIF.

    Parameters
    ----------
    data : bytes
        Encoded image data.

    Returns
    -------
    bool
        True if the data starts with the JPEG magic number.
    """
    return data[:2] == _JPEG_MAGIC


def embed(data: bytes, tags: Mapping[str, str]) -> bytes:
    """Return ``data`` with ``tags`` written into its EXIF.

    Any EXIF already present is replaced, so re-tagging a frame does not
    accumulate segments.  Data that is not a JPEG is returned unchanged
    rather than raising: tagging is a best effort layered on top of
    whatever the camera was asked to encode.

    Parameters
    ----------
    data : bytes
        Encoded image data.
    tags : Mapping[str, str]
        Tags to write.  An empty mapping returns ``data`` unchanged.

    Returns
    -------
    bytes
        The tagged image data.

    Raises
    ------
    ExifError
        If the data is a JPEG but could not be tagged.
    """
    if not tags or not is_jpeg(data):
        return data

    try:
        exif_bytes = _dump(tags)
    except Exception as exc:
        raise ExifError(f"could not build EXIF from {sorted(tags)}: {exc}") from exc

    try:
        sink = io.BytesIO()
        # piexif.insert writes to a file or a BytesIO rather than
        # returning bytes, so it gets a buffer.
        piexif.insert(exif_bytes, data, sink)
        return sink.getvalue()
    except Exception as exc:
        raise ExifError(f"could not write EXIF into JPEG data: {exc}") from exc


def extract(data: bytes) -> dict[str, str]:
    """Read the tags out of image data.

    Never raises: data with no tags, damaged EXIF, or a format that
    carries none all read as no tags.  A caller has an image either way,
    and refusing to show it because its metadata is malformed helps
    nobody.

    Parameters
    ----------
    data : bytes
        Encoded image data.

    Returns
    -------
    dict[str, str]
        The tags found, empty if there were none.
    """
    if not is_jpeg(data):
        return {}

    try:
        loaded = piexif.load(data)
    except Exception:
        return {}

    comment = loaded.get("Exif", {}).get(piexif.ExifIFD.UserComment)
    if isinstance(comment, bytes) and comment.startswith(_ASCII_PREFIX):
        try:
            decoded = json.loads(comment[len(_ASCII_PREFIX) :].decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, dict):
            return {str(k): str(v) for k, v in decoded.items()}

    # No JSON to read, so this is somebody else's JPEG. Recover what the
    # standard tags can tell us.
    tags: dict[str, str] = {}
    for name, (ifd, tag) in STANDARD_TAGS.items():
        raw = loaded.get(ifd, {}).get(tag)
        if isinstance(raw, bytes):
            value = raw.decode("ascii", errors="replace").rstrip("\x00").strip()
            if value:
                tags[name] = value
    return tags


def image_size(data: bytes) -> tuple[int, int] | None:
    """Read pixel dimensions from a JPEG header, without decoding it.

    Not EXIF: this is the container's own declaration of its size, which
    is the authoritative answer for what was actually encoded and costs a
    marker walk rather than a decode.

    Parameters
    ----------
    data : bytes
        Encoded image data.

    Returns
    -------
    tuple[int, int] | None
        Width and height, or ``None`` if the header could not be read.
    """
    if not is_jpeg(data):
        return None

    index = 2
    end = len(data)
    while index + 4 <= end:
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        # Standalone markers: padding, TEM, RSTn, SOI, EOI.
        if marker in (0xFF, 0x01) or 0xD0 <= marker <= 0xD9:
            index += 2
            continue
        (length,) = struct.unpack_from(">H", data, index + 2)
        if marker in _JPEG_SOF_MARKERS:
            if index + 9 > end:
                return None
            height, width = struct.unpack_from(">HH", data, index + 5)
            return int(width), int(height)
        index += 2 + length
    return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _dump(tags: Mapping[str, str]) -> bytes:
    """Serialise tags to EXIF bytes: JSON in ``UserComment``, plus standards.

    Keys are sorted so identical tags produce identical bytes.  That
    matters upstream: an unchanged frame with unchanged tags encodes to
    the same image, which is what lets psygnal suppress a republish.
    """
    text = json.dumps(
        {str(k): str(v) for k, v in tags.items()}, sort_keys=True, ensure_ascii=True
    )
    zeroth: dict[int, bytes] = {}
    exif: dict[int, bytes] = {piexif.ExifIFD.UserComment: _ASCII_PREFIX + text.encode()}

    for name, (ifd, tag) in STANDARD_TAGS.items():
        value = tags.get(name)
        if not value:
            continue
        target = zeroth if ifd == "0th" else exif
        target[tag] = str(value).encode("ascii", errors="replace")

    return piexif.dump(
        {"0th": zeroth, "Exif": exif, "GPS": {}, "1st": {}, "thumbnail": None}
    )


__all__ = [
    "DATETIME_TAG",
    "ExifError",
    "TIMESTAMP_TAG",
    "STANDARD_TAGS",
    "embed",
    "extract",
    "image_size",
    "is_jpeg",
]
