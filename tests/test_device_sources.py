"""Tests for the device-backed sources: V4L2 and network streams."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
import pytest

from SpiriCamera.sources import resolve_source
from SpiriCamera.sources.base import CaptureSettings, SourceError, SourceURL
from SpiriCamera.sources.v4l import V4LSource

from .conftest import CaptureHolder

SETTINGS = CaptureSettings(max_width=1280, max_height=720, max_framerate=25)


class TestV4LClaims:
    """Which strings the V4L2 handler takes."""

    @pytest.mark.parametrize(
        "source", ["/dev/video0", "/dev/video10", "0", "12", "v4l://0", "v4l2:///dev/video1"]
    )
    def test_claims(self, source: str) -> None:
        """Device paths, bare indices, and explicit schemes all match."""
        assert V4LSource.handles(SourceURL.parse(source))

    @pytest.mark.parametrize("source", ["rtsp://host/s", "testimage://", "http://host/f"])
    def test_declines_other_schemes(self, source: str) -> None:
        """A URL belonging to another scheme is left alone."""
        assert not V4LSource.handles(SourceURL.parse(source))

    def test_bare_index_opens_by_number_not_filename(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A bare index must reach cv2 as an int, or it opens a file."""
        holder = fake_capture()
        source = resolve_source("0")

        source.open(SETTINGS)

        assert holder.capture is not None
        assert holder.capture.target == 0
        assert isinstance(holder.capture.target, int)

    def test_device_path_opens_by_path(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A device path is passed through as a string."""
        holder = fake_capture()
        resolve_source("/dev/video0").open(SETTINGS)

        assert holder.capture is not None
        assert holder.capture.target == "/dev/video0"


class TestV4LMetadata:
    """Device identity read from sysfs."""

    @pytest.fixture
    def sysfs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Build a fake /sys/class/video4linux tree with a USB camera."""
        node = tmp_path / "video0"
        node.mkdir()
        (node / "name").write_text("Integrated Camera\n")

        usb = tmp_path / "usb-device"
        usb.mkdir()
        (usb / "idVendor").write_text("04f2\n")
        (usb / "manufacturer").write_text("Acme Optics\n")
        (usb / "product").write_text("HD Webcam C1\n")
        (usb / "serial").write_text("SN-12345\n")
        (node / "device").symlink_to(usb)

        monkeypatch.setattr("SpiriCamera.sources.v4l._SYSFS_ROOT", tmp_path)
        return tmp_path

    def test_reads_usb_identity(self, sysfs: Path) -> None:
        """Vendor, model, and serial come from the owning USB device."""
        metadata = resolve_source("/dev/video0").device_metadata()

        assert metadata["vendor"] == "Acme Optics"
        assert metadata["model"] == "HD Webcam C1"
        assert metadata["serial_number"] == "SN-12345"

    def test_bare_index_finds_the_same_node(self, sysfs: Path) -> None:
        """Index 0 and /dev/video0 describe one device."""
        assert resolve_source("0").device_metadata()["model"] == "HD Webcam C1"

    def test_falls_back_to_node_name(self, sysfs: Path) -> None:
        """Without USB attributes, the V4L2 node name is the model."""
        (sysfs / "video0" / "device").unlink()

        metadata = resolve_source("/dev/video0").device_metadata()

        assert metadata["model"] == "Integrated Camera"
        assert "vendor" not in metadata

    def test_missing_node_reports_nothing(self, sysfs: Path) -> None:
        """An unknown device yields no metadata rather than an error."""
        assert resolve_source("/dev/video9").device_metadata() == {}

    def test_metadata_reaches_capabilities(
        self, sysfs: Path, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Identity is reported through the capabilities record."""
        fake_capture()

        capabilities = resolve_source("/dev/video0").open(SETTINGS)

        assert capabilities.vendor == "Acme Optics"
        assert capabilities.serial_number == "SN-12345"


class TestOpenCVSourceLifecycle:
    """Behaviour shared by the V4L2 and network handlers."""

    def test_applies_requested_settings(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """The bounds are pushed onto the device at open time."""
        holder = fake_capture()
        resolve_source("/dev/video0").open(SETTINGS)

        assert holder.capture is not None
        assert holder.capture.properties[cv2.CAP_PROP_FRAME_WIDTH] == 1280
        assert holder.capture.properties[cv2.CAP_PROP_FRAME_HEIGHT] == 720
        assert holder.capture.properties[cv2.CAP_PROP_FPS] == 25

    def test_reports_what_the_device_negotiated(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Capabilities reflect the device, not the request."""
        fake_capture(width=640, height=480, framerate=15)

        capabilities = resolve_source("/dev/video0").open(SETTINGS)

        assert capabilities.max_supported_width == 640
        assert capabilities.max_supported_height == 480
        assert capabilities.max_supported_framerate == 15

    def test_failure_to_open_raises(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A device that will not open is a SourceError."""
        fake_capture(opened=False)

        with pytest.raises(SourceError, match="Failed to open"):
            resolve_source("/dev/video0").open(SETTINGS)

    def test_open_is_idempotent(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """Opening twice does not replace a working capture."""
        holder = fake_capture()
        source = resolve_source("/dev/video0")
        source.open(SETTINGS)
        first = holder.capture

        source.open(SETTINGS)

        assert holder.capture is first

    def test_close_releases(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """Closing releases the device and clears is_open."""
        holder = fake_capture()
        source = resolve_source("/dev/video0")
        source.open(SETTINGS)

        source.close()

        assert holder.capture is not None
        assert holder.capture.released
        assert not source.is_open

    def test_close_without_open_is_harmless(self) -> None:
        """Closing a source that never opened does nothing."""
        resolve_source("/dev/video0").close()

    def test_read_before_open_raises(self) -> None:
        """Reading an unopened source is a programming error."""
        with pytest.raises(SourceError, match="not open"):
            resolve_source("/dev/video0").read(SETTINGS)

    def test_read_returns_frame(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """A successful grab returns the device's array."""
        frame = np.full((480, 640, 3), 7, np.uint8)
        fake_capture(frames=[frame])
        source = resolve_source("/dev/video0")
        source.open(SETTINGS)

        assert source.read(SETTINGS) is frame

    def test_dropped_frame_returns_none(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A failed grab is a transient None, not an exception."""
        fake_capture(frames=[None])
        source = resolve_source("/dev/video0")
        source.open(SETTINGS)

        assert source.read(SETTINGS) is None


class TestNetworkSource:
    """Stream-specific behaviour."""

    @pytest.mark.parametrize(
        "url",
        [
            "rtsp://user:pass@10.0.0.5:554/stream",
            "rtmp://host/live/key",
            "http://host/feed.mjpg",
            "https://host/feed.mjpg",
        ],
    )
    def test_passes_the_whole_url_to_opencv(
        self, url: str, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """The scheme is part of what FFmpeg needs, so nothing is stripped."""
        holder = fake_capture()
        resolve_source(url).open(SETTINGS)

        assert holder.capture is not None
        assert holder.capture.target == url

    def test_rejects_hostless_url(self) -> None:
        """A scheme with no host cannot be opened, so it fails early."""
        with pytest.raises(SourceError, match="No host given"):
            resolve_source("rtsp://")
