"""Tests for frame tags carried inside the encoded frame."""

from __future__ import annotations

import io
import struct

import cv2
import numpy as np
import piexif
import pytest

from SpiriCamera import exif


@pytest.fixture
def jpeg() -> bytes:
    """An untagged JPEG, as OpenCV produces one.

    Returns
    -------
    bytes
        A 64x48 black JPEG.
    """
    frame = np.zeros((48, 64, 3), np.uint8)
    return cv2.imencode(".jpg", frame)[1].tobytes()


class TestRoundTrip:
    """Writing tags and reading them back."""

    def test_tags_survive(self, jpeg: bytes) -> None:
        """What goes in comes back out unchanged."""
        tags = {"make": "Spiri", "mission": "probe-1", "timestamp": "1789000000.5"}

        assert exif.extract(exif.embed(jpeg, tags)) == tags

    def test_arbitrary_keys_survive(self, jpeg: bytes) -> None:
        """A key with no standard EXIF equivalent round-trips anyway."""
        tags = {"gimbal_pitch": "-42.5", "operator": "alex"}

        assert exif.extract(exif.embed(jpeg, tags)) == tags

    def test_values_are_stringified(self, jpeg: bytes) -> None:
        """Tags are a str-to-str mapping, whatever the caller passes."""
        tagged = exif.embed(jpeg, {"count": 7})  # type: ignore[dict-item]

        assert exif.extract(tagged) == {"count": "7"}

    def test_non_ascii_survives(self, jpeg: bytes) -> None:
        """JSON escapes what the ASCII UserComment cannot hold directly."""
        tagged = exif.embed(jpeg, {"operator": "Ëlise"})

        assert exif.extract(tagged) == {"operator": "Ëlise"}

    def test_the_image_still_decodes(self, jpeg: bytes) -> None:
        """Tagging does not damage the picture."""
        tagged = exif.embed(jpeg, {"mission": "probe-1"})
        decoded = cv2.imdecode(np.frombuffer(tagged, np.uint8), cv2.IMREAD_COLOR)

        assert decoded is not None
        assert decoded.shape == (48, 64, 3)

    def test_retagging_replaces(self, jpeg: bytes) -> None:
        """Tagging twice does not accumulate segments or tags."""
        once = exif.embed(jpeg, {"mission": "probe-1"})
        twice = exif.embed(once, {"mission": "probe-2"})

        assert exif.extract(twice) == {"mission": "probe-2"}
        assert len(twice) == len(once)

    def test_identical_tags_encode_identically(self, jpeg: bytes) -> None:
        """Deterministic bytes are what lets psygnal suppress a republish."""
        tags = {"b": "2", "a": "1"}

        assert exif.embed(jpeg, tags) == exif.embed(jpeg, dict(reversed(tags.items())))


class TestStandardTags:
    """Interoperating with tools that read real EXIF tags."""

    def test_known_keys_reach_their_exif_tag(self, jpeg: bytes) -> None:
        """An ordinary image tool sees a sensible Make and Model."""
        tagged = exif.embed(jpeg, {"make": "Spiri", "model": "SC-1"})
        loaded = piexif.load(tagged)

        assert loaded["0th"][piexif.ImageIFD.Make] == b"Spiri"
        assert loaded["0th"][piexif.ImageIFD.Model] == b"SC-1"

    def test_foreign_exif_is_recovered(self, jpeg: bytes) -> None:
        """A JPEG this package did not write still yields what it can."""
        foreign = piexif.dump(
            {"0th": {piexif.ImageIFD.Make: b"Somebody Else"}, "Exif": {}}
        )
        sink = io.BytesIO()
        piexif.insert(foreign, jpeg, sink)

        assert exif.extract(sink.getvalue()) == {"make": "Somebody Else"}


class TestRefusals:
    """What happens to data that cannot carry tags."""

    def test_empty_tags_change_nothing(self, jpeg: bytes) -> None:
        """Nothing to say means the frame is left exactly as it was."""
        assert exif.embed(jpeg, {}) is jpeg

    def test_non_jpeg_is_returned_untouched(self) -> None:
        """Tagging is best effort on top of whatever was encoded."""
        png = cv2.imencode(".png", np.zeros((4, 4, 3), np.uint8))[1].tobytes()

        assert exif.embed(png, {"mission": "probe-1"}) is png
        assert exif.extract(png) == {}

    def test_damaged_exif_reads_as_untagged(self, jpeg: bytes) -> None:
        """A broken tag is not a reason to withhold a usable image."""
        tagged = bytearray(exif.embed(jpeg, {"mission": "probe-1"}))
        tagged[20:40] = b"\x00" * 20

        assert exif.extract(bytes(tagged)) == {}

    def test_empty_data_reads_as_untagged(self) -> None:
        """No image at all is not an error either."""
        assert exif.extract(b"") == {}
        assert exif.image_size(b"") is None

    def test_truncated_jpeg_raises_on_embed(self) -> None:
        """A JPEG that cannot be spliced says so rather than corrupting."""
        with pytest.raises(exif.ExifError):
            exif.embed(b"\xff\xd8nonsense", {"mission": "probe-1"})


class TestImageSize:
    """Reading dimensions out of the container header."""

    @pytest.mark.parametrize(
        ("width", "height"), [(64, 48), (1, 1), (1920, 1080), (3, 7)]
    )
    def test_reports_encoded_dimensions(self, width: int, height: int) -> None:
        """The header is read without decoding the picture."""
        frame = np.zeros((height, width, 3), np.uint8)
        data = cv2.imencode(".jpg", frame)[1].tobytes()

        assert exif.image_size(data) == (width, height)

    def test_survives_tagging(self, jpeg: bytes) -> None:
        """An inserted APP1 segment does not hide the frame header."""
        tagged = exif.embed(jpeg, {"mission": "probe-1"})

        assert exif.image_size(tagged) == (64, 48)

    def test_non_jpeg_reports_nothing(self) -> None:
        """Only JPEG is claimed."""
        png = cv2.imencode(".png", np.zeros((4, 4, 3), np.uint8))[1].tobytes()

        assert exif.image_size(png) is None

    def test_header_without_a_frame_reports_nothing(self) -> None:
        """A JPEG that stops before its SOF is not guessed at."""
        assert exif.image_size(b"\xff\xd8\xff\xfe\x00\x04ab") is None


class TestMalformedInput:
    """Parsing is best effort; nothing here may raise."""

    def test_data_that_is_not_a_marker_stream(self) -> None:
        """Bytes after the magic number that are not markers stop the walk."""
        assert exif.image_size(b"\xff\xd8not markers at all") is None

    def test_standalone_markers_are_stepped_over(self, jpeg: bytes) -> None:
        """RSTn and friends carry no length field to skip by."""
        padded = b"\xff\xd8\xff\xd0\xff\x01" + jpeg[2:]

        assert exif.image_size(padded) == (64, 48)

    def test_a_truncated_frame_header(self) -> None:
        """A SOF0 that stops mid-header is not guessed at."""
        assert exif.image_size(b"\xff\xd8\xff\xc0\x00\x11\x08") is None

    def test_garbage_in_the_exif_segment(self, jpeg: bytes) -> None:
        """An APP1 that announces EXIF and then lies reads as untagged."""
        payload = b"Exif\x00\x00" + b"\xff" * 32
        segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload

        assert exif.extract(jpeg[:2] + segment + jpeg[2:]) == {}

    def test_a_user_comment_that_is_not_json(self, jpeg: bytes) -> None:
        """Falls through to the standard tags rather than raising."""
        raw = piexif.dump(
            {
                "0th": {piexif.ImageIFD.Make: b"Spiri"},
                "Exif": {piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00not json"},
            }
        )
        sink = io.BytesIO()
        piexif.insert(raw, jpeg, sink)

        assert exif.extract(sink.getvalue()) == {"make": "Spiri"}

    def test_a_tag_that_cannot_be_serialised(self, jpeg: bytes) -> None:
        """An unrepresentable tag is an ExifError, not a traceback."""

        class Awkward:
            def __str__(self) -> str:
                raise RuntimeError("no")

        with pytest.raises(exif.ExifError, match="could not build EXIF"):
            exif.embed(jpeg, {"awkward": Awkward()})  # type: ignore[dict-item]
