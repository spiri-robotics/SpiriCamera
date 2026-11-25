"""Tests for the device-backed sources: V4L2 and network streams."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
import pytest

from SpiriCamera.sources import resolve_source
from SpiriCamera.sources.base import CaptureSettings, SourceError, SourceURL
from SpiriCamera.sources.capture import FFMPEG_OPTIONS_ENV, merge_ffmpeg_options
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

    @pytest.mark.parametrize("source", ["rtsp://host/s", "testimage://", "http://host/f"])
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
        monkeypatch.setattr(
            "SpiriCamera.sources.v4l._SYSFS_ROOT", tmp_path / "sys/class/video4linux"
        )
        assert V4LSource.handles(SourceURL.parse("/dev/null"))

    def test_no_video4linux_class_means_no_cameras(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With sysfs mounted but no video4linux class, nothing is a camera.

        That's a host with no camera driver loaded, like a CI runner: a
        character device such as /dev/null is still not a camera there.
        """
        monkeypatch.setattr("SpiriCamera.sources.v4l._SYSFS_ROOT", tmp_path / "video4linux")
        assert not V4LSource.handles(SourceURL.parse("/dev/null"))

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

    def test_device_path_opens_by_path(self, fake_capture: Callable[..., CaptureHolder]) -> None:
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

    def test_applies_requested_settings(self, fake_capture: Callable[..., CaptureHolder]) -> None:
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

    def test_failure_to_open_raises(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """A device that will not open is a SourceError."""
        fake_capture(opened=False)

        with pytest.raises(SourceError, match="Failed to open"):
            resolve_source("v4l:///dev/video0").open(SETTINGS)

    def test_open_is_idempotent(self, fake_capture: Callable[..., CaptureHolder]) -> None:
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

    def test_read_returns_frame(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """A successful grab returns the device's array."""
        frame = np.full((480, 640, 3), 7, np.uint8)
        fake_capture(frames=[frame])
        source = resolve_source("v4l:///dev/video0")
        source.open(SETTINGS)

        assert source.read(SETTINGS) is frame

    def test_dropped_frame_returns_none(self, fake_capture: Callable[..., CaptureHolder]) -> None:
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

    def test_bounds_ffmpeg_s_read_timeout(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """FFmpeg's default waits 30 seconds per read of a dead stream."""
        holder = fake_capture()
        resolve_source("rtsp://10.0.0.5/stream").open(SETTINGS)

        params = dict(zip(holder.params[::2], holder.params[1::2], strict=True))
        assert params[cv2.CAP_PROP_READ_TIMEOUT_MSEC] == 5000

    def test_a_dropped_frame_is_not_a_lost_stream(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """One failed read on a stream that was just delivering is a dropped frame."""
        holder = fake_capture(frames=[None])
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(SETTINGS)

        assert source.read(SETTINGS) is None
        assert holder.capture is not None and not holder.capture.released

    def test_a_stalled_stream_closes_itself(
        self, fake_capture: Callable[..., CaptureHolder], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An RTSP server that dropped the session never sends again on
        that capture, so the source closes for the camera to reconnect."""
        holder = fake_capture(frames=[None])
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(SETTINGS)
        source._last_frame_at = time.monotonic() - 6  # type: ignore[attr-defined]

        with pytest.raises(SourceError, match="no frame for 6s"):
            source.read(SETTINGS)
        assert not source.is_open
        assert holder.capture is not None and holder.capture.released

    def test_a_frame_that_waited_out_the_timeout_is_stale(
        self, fake_capture: Callable[..., CaptureHolder], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the server drops the session, FFmpeg still hands back a
        few buffered frames, each only after the full read timeout."""
        monkeypatch.setattr("SpiriCamera.sources.network._STALL_TIMEOUT", 0.02)
        fake_capture(delay=0.05)
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(SETTINGS)
        source._last_frame_at = time.monotonic() - 1  # type: ignore[attr-defined]

        with pytest.raises(SourceError, match="reconnecting"):
            source.read(SETTINGS)


class TestFFmpegOptions:
    """A stream opens with low-latency FFmpeg options, under the deployment's."""

    @pytest.fixture
    def seen(self, monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
        """What OPENCV_FFMPEG_CAPTURE_OPTIONS said at each open."""
        seen: list[str | None] = []

        def factory(target: object, *args: object) -> object:
            seen.append(os.environ.get(FFMPEG_OPTIONS_ENV))
            from .conftest import FakeCapture

            return FakeCapture(target)

        monkeypatch.setattr("SpiriCamera.sources.capture.cv2.VideoCapture", factory)
        return seen

    def test_rtsp_opens_with_low_latency_options(
        self, seen: list[str | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing configured: the source's own, and the variable left unset after."""
        monkeypatch.delenv(FFMPEG_OPTIONS_ENV, raising=False)
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(SETTINGS)
        source.close()

        assert seen == ["fflags;nobuffer|max_delay;0|buffer_size;262144"]
        assert FFMPEG_OPTIONS_ENV not in os.environ

    def test_the_deployment_s_options_win(
        self, seen: list[str | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drone's ``rtsp_transport;udp`` is kept, and its own buffer size too."""
        monkeypatch.setenv(FFMPEG_OPTIONS_ENV, "rtsp_transport;udp|buffer_size;1000000")
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(SETTINGS)
        source.close()

        options = dict(entry.split(";") for entry in (seen[0] or "").split("|"))
        assert options == {
            "fflags": "nobuffer",
            "max_delay": "0",
            "buffer_size": "1000000",
            "rtsp_transport": "udp",
        }
        assert os.environ[FFMPEG_OPTIONS_ENV] == "rtsp_transport;udp|buffer_size;1000000"

    def test_other_streams_get_no_rtsp_options(self, seen: list[str | None]) -> None:
        """An HTTP stream has no RTP to reorder or UDP buffer to size."""
        source = resolve_source("http://host/feed.mjpg")
        source.open(SETTINGS)
        source.close()

        assert seen[0] is not None and seen[0].startswith("fflags;nobuffer")
        assert "max_delay" not in seen[0]

    def test_a_file_gets_none(self, seen: list[str | None], tmp_path: Path) -> None:
        """A file isn't a stream: whatever the deployment set reaches it untouched."""
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"")
        source = resolve_source(f"file://{video}")
        with contextlib.suppress(SourceError):
            source.open(SETTINGS)
        source.close()

        assert all(options is None or "nobuffer" not in options for options in seen)

    def test_entries_without_a_value_are_kept(self) -> None:
        """OpenCV's format is the deployment's to use as it likes."""
        assert merge_ffmpeg_options({"a": "1"}, "odd|b;2") == "a;1|b;2|odd"


class TestLiveSources:
    """A live source keeps up with its device, so frames never queue."""

    def test_only_streams_and_cameras_are_live(self, tmp_path: Path) -> None:
        """A file or test pattern is read when asked, so the camera paces it."""
        assert resolve_source("rtsp://10.0.0.5/stream").live
        assert resolve_source("v4l:///dev/video0").live
        image = tmp_path / "still.png"
        cv2.imwrite(str(image), np.zeros((8, 8, 3), np.uint8))
        assert not resolve_source(f"file://{image}").live
        assert not resolve_source("testimage://").live

    def test_hands_back_the_newest_frame_not_the_oldest(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Frames the reader didn't get to are dropped, not queued for later."""
        frames = [np.full((4, 4, 3), n, np.uint8) for n in range(256)]
        fake_capture(frames=frames)
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(CaptureSettings(max_framerate=0))
        time.sleep(0.1)

        frame = source.read(CaptureSettings(max_framerate=0))

        assert frame is not None
        # About a hundred grabs in 0.1 s; a queue would hand back the first.
        assert frame[0, 0, 0] > 20

    def test_a_slow_reader_does_not_fall_behind(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """The regression: a reader slower than the device gets ever-older frames."""
        frames = [np.full((4, 4, 3), n % 256, np.uint8) for n in range(100_000)]
        holder = fake_capture(frames=frames, delay=0.01)
        source = resolve_source("rtsp://10.0.0.5/stream")
        settings = CaptureSettings(max_framerate=0)
        source.open(settings)
        assert holder.capture is not None

        for _ in range(10):
            source.read(settings)
            time.sleep(0.03)  # a pass three frames long
        source.read(settings)

        # Handed back the frame the device just sent, not the 11th.
        assert holder.capture._reads - source._last_grab.seq <= 2  # type: ignore[union-attr]

    def test_holds_to_max_framerate_by_dropping_frames(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A 100 fps device read at max_framerate=20 gives 20 frames a second."""
        frames = [np.full((4, 4, 3), n % 256, np.uint8) for n in range(100_000)]
        fake_capture(frames=frames, delay=0.01)
        source = resolve_source("rtsp://10.0.0.5/stream")
        settings = CaptureSettings(max_framerate=20)
        source.open(settings)

        started = time.monotonic()
        count = 0
        while time.monotonic() - started < 1.0:
            if source.read(settings) is not None:
                count += 1

        assert 17 <= count <= 22

    def test_keeps_a_matching_framerate_despite_jitter(
        self, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A device at exactly max_framerate loses no frames for arriving a little early."""
        fake_capture(delay=0.02)
        source = resolve_source("rtsp://10.0.0.5/stream")
        settings = CaptureSettings(max_framerate=50)
        source.open(settings)

        started = time.monotonic()
        count = 0
        while time.monotonic() - started < 1.0:
            if source.read(settings) is not None:
                count += 1

        # The fake's 20 ms reads run a little long, so a little under 50.
        assert count >= 40

    def test_close_stops_the_grabber(self, fake_capture: Callable[..., CaptureHolder]) -> None:
        """The capture is released only once nothing is reading it."""
        holder = fake_capture()
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(SETTINGS)
        grabber = source._grabber  # type: ignore[attr-defined]
        assert grabber is not None and grabber.is_alive()

        source.close()

        assert not grabber.is_alive()
        assert holder.capture is not None and holder.capture.released

    def test_a_quiet_stream_returns_none_after_a_short_wait(
        self, fake_capture: Callable[..., CaptureHolder], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a camera that's stopping isn't held up by a stream that sends nothing."""
        monkeypatch.setattr("SpiriCamera.sources.capture._LIVE_READ_WAIT", 0.05)
        fake_capture(delay=1.0)
        source = resolve_source("rtsp://10.0.0.5/stream")
        source.open(SETTINGS)

        started = time.monotonic()
        assert source.read(SETTINGS) is None
        assert time.monotonic() - started < 0.5
