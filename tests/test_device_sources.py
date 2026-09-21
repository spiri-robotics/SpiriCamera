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

    @pytest.fixture
    def sysfs_knows_null(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make sysfs report /dev/null as a video device.

        /dev/null is a character device on every system, which makes it
        a portable stand-in for a camera node: tests cannot create one,
        since mknod needs root.
        """
        (tmp_path / "null").mkdir()
        monkeypatch.setattr("SpiriCamera.sources.v4l._SYSFS_ROOT", tmp_path)

    @pytest.mark.parametrize(
        "source", ["0", "12", "v4l://0", "v4l2:///dev/video1", "v4l:///dev/video10"]
    )
    def test_claims(self, source: str) -> None:
        """Bare indices and explicit schemes match without a filesystem."""
        assert V4LSource.handles(SourceURL.parse(source))

    def test_claims_a_device_node(self, sysfs_knows_null: None) -> None:
        """A schemeless path is claimed when Linux calls it a camera."""
        assert V4LSource.handles(SourceURL.parse("/dev/null"))

    @pytest.mark.parametrize(
        "source", ["rtsp://host/s", "testimage://", "http://host/f"]
    )
    def test_declines_other_schemes(self, source: str) -> None:
        """A URL belonging to another scheme is left alone."""
        assert not V4LSource.handles(SourceURL.parse(source))

    def test_declines_a_character_device_that_is_not_a_camera(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Being a character device is not enough; sysfs has to agree.

        Otherwise /dev/null and friends would be claimed and then fail
        to open.
        """
        monkeypatch.setattr("SpiriCamera.sources.v4l._SYSFS_ROOT", tmp_path)
        assert not V4LSource.handles(SourceURL.parse("/dev/null"))

    def test_declines_a_regular_file(self, tmp_path: Path) -> None:
        """A file is a file, even one named like a device."""
        path = tmp_path / "video0"
        path.write_bytes(b"")
        assert not V4LSource.handles(SourceURL.parse(str(path)))

    def test_declines_a_half_typed_path(self) -> None:
        """A path that does not exist is not claimed and then failed.

        Claiming it would leave the camera stopped with nothing to
        report but a failure to open.
        """
        assert not V4LSource.handles(SourceURL.parse("/dev/vi"))

    def test_trusts_the_device_node_without_sysfs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where sysfs is not mounted, a character device is enough.

        Consulting it is then impossible rather than merely unhelpful,
        which is the situation inside a container without /sys.
        """
        monkeypatch.setattr("SpiriCamera.sources.v4l._SYSFS_ROOT", tmp_path / "absent")
        assert V4LSource.handles(SourceURL.parse("/dev/null"))

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
        resolve_source("v4l:///dev/video0").open(SETTINGS)

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
        metadata = resolve_source("v4l:///dev/video0").device_metadata()

        assert metadata["vendor"] == "Acme Optics"
        assert metadata["model"] == "HD Webcam C1"
        assert metadata["serial_number"] == "SN-12345"

    def test_bare_index_finds_the_same_node(self, sysfs: Path) -> None:
        """Index 0 and /dev/video0 describe one device."""
        assert resolve_source("0").device_metadata()["model"] == "HD Webcam C1"

    def test_falls_back_to_node_name(self, sysfs: Path) -> None:
        """Without USB attributes, the V4L2 node name is the model."""
        (sysfs / "video0" / "device").unlink()

        metadata = resolve_source("v4l:///dev/video0").device_metadata()

        assert metadata["model"] == "Integrated Camera"
        assert "vendor" not in metadata

    def test_missing_node_reports_nothing(self, sysfs: Path) -> None:
        """An unknown device yields no metadata rather than an error."""
        assert resolve_source("v4l:///dev/video9").device_metadata() == {}

    def test_metadata_reaches_capabilities(
        self, sysfs: Path, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Identity is reported through the capabilities record."""
        fake_capture()

        capabilities = resolve_source("v4l:///dev/video0").open(SETTINGS)

        assert capabilities.vendor == "Acme Optics"
        assert capabilities.serial_number == "SN-12345"


class TestOpenCVSourceLifecycle:
    """Behaviour shared by the V4L2 and network handlers."""

    def test_applies_requested_settings(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """The bounds are pushed onto the device at open time."""
        holder = fake_capture()
        resolve_source("v4l:///dev/video0").open(SETTINGS)

        assert holder.capture is not None
        assert holder.capture.properties[cv2.CAP_PROP_FRAME_WIDTH] == 1280
        assert holder.capture.properties[cv2.CAP_PROP_FRAME_HEIGHT] == 720
        assert holder.capture.properties[cv2.CAP_PROP_FPS] == 25

    def test_reports_what_the_device_negotiated(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Capabilities reflect the device, not the request."""
        fake_capture(width=640, height=480, framerate=15)

        capabilities = resolve_source("v4l:///dev/video0").open(SETTINGS)

        assert capabilities.max_supported_width == 640
        assert capabilities.max_supported_height == 480
        assert capabilities.max_supported_framerate == 15

    def test_failure_to_open_raises(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A device that will not open is a SourceError."""
        fake_capture(opened=False)

        with pytest.raises(SourceError, match="Failed to open"):
            resolve_source("v4l:///dev/video0").open(SETTINGS)

    def test_open_is_idempotent(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Opening twice does not replace a working capture."""
        holder = fake_capture()
        source = resolve_source("v4l:///dev/video0")
        source.open(SETTINGS)
        first = holder.capture

        source.open(SETTINGS)

        assert holder.capture is first

    def test_close_releases(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """Closing releases the device and clears is_open."""
        holder = fake_capture()
        source = resolve_source("v4l:///dev/video0")
        source.open(SETTINGS)

        source.close()

        assert holder.capture is not None
        assert holder.capture.released
        assert not source.is_open

    def test_close_without_open_is_harmless(self) -> None:
        """Closing a source that never opened does nothing."""
        resolve_source("v4l:///dev/video0").close()

    def test_read_before_open_raises(self) -> None:
        """Reading an unopened source is a programming error."""
        with pytest.raises(SourceError, match="not open"):
            resolve_source("v4l:///dev/video0").read(SETTINGS)

    def test_read_returns_frame(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A successful grab returns the device's array."""
        frame = np.full((480, 640, 3), 7, np.uint8)
        fake_capture(frames=[frame])
        source = resolve_source("v4l:///dev/video0")
        source.open(SETTINGS)

        assert source.read(SETTINGS) is frame

    def test_dropped_frame_returns_none(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A failed grab is a transient None, not an exception."""
        fake_capture(frames=[None])
        source = resolve_source("v4l:///dev/video0")
        source.open(SETTINGS)

        assert source.read(SETTINGS) is None

    def test_a_changed_resolution_is_reapplied_while_running(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Lowering max_width/max_height must not wait for a restart.

        The settings a running camera hands to read() come from live,
        rebindable fields, so a value picked after start() has to reach
        the device on the very next frame.
        """
        holder = fake_capture()
        source = resolve_source("v4l:///dev/video0")
        source.open(SETTINGS)
        assert holder.capture is not None
        holder.capture.properties.clear()

        smaller = CaptureSettings(max_width=320, max_height=240, max_framerate=25)
        source.read(smaller)

        assert holder.capture.properties[cv2.CAP_PROP_FRAME_WIDTH] == 320
        assert holder.capture.properties[cv2.CAP_PROP_FRAME_HEIGHT] == 240

    def test_repeating_the_same_settings_does_not_reapply(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """An unchanged request should not re-touch the device every frame."""
        holder = fake_capture()
        source = resolve_source("v4l:///dev/video0")
        source.open(SETTINGS)
        assert holder.capture is not None
        holder.capture.properties.clear()

        source.read(SETTINGS)

        assert holder.capture.properties == {}


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
