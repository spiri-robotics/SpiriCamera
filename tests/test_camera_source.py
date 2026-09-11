"""Tests for CameraSource URL parsing."""

from __future__ import annotations

import pytest

from SpiriCamera.camera import CameraSource


class TestCameraSourceParse:
    """Test URL parsing logic for various source formats."""

    def test_parse_v4l_device(self) -> None:
        """Test /dev/video0 style V4L2 device paths."""
        src = CameraSource.parse("/dev/video0")
        assert src.source_type == "local"
        assert src.scheme == "v4l"
        assert src.path == "/dev/video0"
        assert src.camera_index == 0
        assert src.source == "/dev/video0"
        assert src.is_valid is True

    def test_parse_v4l_device_high_index(self) -> None:
        """Test /dev/video10 style V4L2 device paths."""
        src = CameraSource.parse("/dev/video10")
        assert src.source_type == "local"
        assert src.scheme == "v4l"
        assert src.path == "/dev/video10"
        assert src.camera_index == 10
        assert src.source == "/dev/video10"

    def test_parse_numeric_index(self) -> None:
        """Test bare numeric camera index."""
        src = CameraSource.parse("0")
        assert src.source_type == "local"
        assert src.scheme == "v4l"
        assert src.path == "0"
        assert src.camera_index == 0
        assert src.source == "0"

    def test_parse_numeric_index_high(self) -> None:
        """Test bare numeric camera index greater than 0."""
        src = CameraSource.parse("2")
        assert src.source_type == "local"
        assert src.scheme == "v4l"
        assert src.path == "2"
        assert src.camera_index == 2
        assert src.source == "2"

    def test_parse_rtsp(self) -> None:
        """Test rtsp:// URLs."""
        src = CameraSource.parse("rtsp://admin:pass@192.168.1.100:554/stream")
        assert src.source_type == "network"
        assert src.scheme == "rtsp"
        assert src.path == "rtsp://admin:pass@192.168.1.100:554/stream"
        assert src.camera_index is None
        assert src.source == "rtsp://admin:pass@192.168.1.100:554/stream"

    def test_parse_rtsp_simple(self) -> None:
        """Test simple rtsp:// URL without credentials."""
        src = CameraSource.parse("rtsp://10.0.0.5:554/live")
        assert src.source_type == "network"
        assert src.scheme == "rtsp"
        assert src.camera_index is None

    def test_parse_rtmp(self) -> None:
        """Test rtmp:// URLs."""
        src = CameraSource.parse("rtmp://live.example.com/app/stream")
        assert src.source_type == "network"
        assert src.scheme == "rtmp"
        assert src.path == "rtmp://live.example.com/app/stream"
        assert src.camera_index is None
        assert src.source == "rtmp://live.example.com/app/stream"

    def test_parse_http(self) -> None:
        """Test http:// image URLs."""
        src = CameraSource.parse("http://10.0.0.5/image.jpg")
        assert src.source_type == "network"
        assert src.scheme == "http"
        assert src.path == "http://10.0.0.5/image.jpg"
        assert src.camera_index is None
        assert src.source == "http://10.0.0.5/image.jpg"

    def test_parse_https(self) -> None:
        """Test https:// image URLs."""
        src = CameraSource.parse("https://secure.example.com/cam/feed")
        assert src.source_type == "network"
        assert src.scheme == "https"
        assert src.path == "https://secure.example.com/cam/feed"
        assert src.camera_index is None
        assert src.source == "https://secure.example.com/cam/feed"

    def test_parse_empty_string(self) -> None:
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            CameraSource.parse("")

    def test_parse_whitespace_only(self) -> None:
        """Test that whitespace-only string raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty|Unrecognised source"):
            CameraSource.parse("   ")

    def test_parse_unknown_scheme(self) -> None:
        """Test that unknown schemes raise ValueError."""
        with pytest.raises(ValueError, match="Unrecognised source"):
            CameraSource.parse("ftp://server/stream")

    def test_parse_plain_text(self) -> None:
        """Test that non-standard text raises ValueError."""
        with pytest.raises(ValueError, match="Unrecognised source"):
            CameraSource.parse("not-a-valid-source")

    def test_parse_with_leading_trailing_whitespace(self) -> None:
        """Test that leading/trailing whitespace is stripped."""
        src = CameraSource.parse("  /dev/video0  ")
        assert src.source == "/dev/video0"
        assert src.path == "/dev/video0"

    def test_invalid_v4l_path(self) -> None:
        """Test non-conforming V4L paths raise ValueError."""
        with pytest.raises(ValueError, match="Unrecognised source"):
            CameraSource.parse("/dev/video")

    def test_negative_index(self) -> None:
        """Test negative indices raise ValueError."""
        with pytest.raises(ValueError, match="Unrecognised source"):
            CameraSource.parse("-1")

    def test_parse_testimage_default(self) -> None:
        """Test testimage:// without image name uses default pm5544."""
        src = CameraSource.parse("testimage://")
        assert src.source_type == "local"
        assert src.scheme == "testimage"
        assert src.path == "pm5544"
        assert src.camera_index is None
        assert src.source == "testimage://"
        assert src.is_valid is True

    def test_parse_testimage_named(self) -> None:
        """Test testimage:// with specific image name."""
        src = CameraSource.parse("testimage://pm5544")
        assert src.source_type == "local"
        assert src.scheme == "testimage"
        assert src.path == "pm5544"
        assert src.camera_index is None
        assert src.source == "testimage://pm5544"

    def test_parse_testimage_with_whitespace(self) -> None:
        """Test testimage:// with trailing whitespace."""
        src = CameraSource.parse("testimage://  ")
        assert src.path == "pm5544"
        assert src.source == "testimage://"

    def test_parse_testimage_unknown(self) -> None:
        """Test testimage:// with unknown image name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown test image"):
            CameraSource.parse("testimage://nonexistent")

