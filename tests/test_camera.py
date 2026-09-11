"""Tests for the Camera lifecycle, encoding, and synced state."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
import pytest

from SpiriCamera.camera import (
    STATUS_RUNNING,
    STATUS_STOPPED,
    Camera,
    CameraError,
    CameraNotStartedError,
    FrameUnavailableError,
    topic_for_source,
)
from SpiriCamera.sources.base import SourceInfo

from .conftest import CaptureHolder

CameraFactory = Callable[..., Camera]


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """Poll until a predicate holds or the timeout expires.

    Parameters
    ----------
    predicate : Callable[[], bool]
        Condition to wait for.
    timeout : float
        Seconds to wait before giving up.

    Returns
    -------
    bool
        Whether the predicate became true.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestTopics:
    """Deriving a SpiriSynq topic from a source string."""

    @pytest.mark.parametrize(
        ("source", "topic"),
        [
            ("/dev/video0", "spiricamera_dev_video0"),
            ("testimage://pm5544", "spiricamera_testimage_pm5544"),
            ("rtsp://host/stream", "spiricamera_rtsp_host_stream"),
            ("", "spiricamera_camera"),
        ],
    )
    def test_topic_for_source(self, source: str, topic: str) -> None:
        """Topics are slugified and always non-empty."""
        assert topic_for_source(source) == topic

    def test_explicit_topic_wins(self, camera: CameraFactory) -> None:
        """A caller can name the topic instead of deriving it."""
        assert camera("testimage://", synq_topic="my_camera").synq_topic == "my_camera"


class TestConstruction:
    """Creating a camera."""

    def test_resolves_its_source(self, camera: CameraFactory) -> None:
        """The parsed description is available before starting."""
        cam = camera("testimage://pm5544")
        assert cam.source_str == "testimage://pm5544"
        assert cam.source.scheme == "testimage"
        assert cam.source.handler == "TestImageSource"
        assert cam.source.error == ""
        assert cam.handler is not None

    def test_starts_stopped_and_empty(self, camera: CameraFactory) -> None:
        """No device is touched and no frame exists until start()."""
        cam = camera("testimage://")
        assert cam.running is False
        assert cam.image == b""
        assert cam.received_width == 0

    def test_settings_are_stored(self, camera: CameraFactory) -> None:
        """Constructor arguments land on the synced fields."""
        cam = camera(
            "testimage://", quality=55, max_width=320, max_height=240, max_framerate=12
        )
        assert (cam.quality, cam.max_width, cam.max_height, cam.max_framerate) == (
            55,
            320,
            240,
            12,
        )

    @pytest.mark.parametrize("source", ["", "   ", "bogus://x", "testimage://nope"])
    def test_bad_source_is_not_fatal(self, camera: CameraFactory, source: str) -> None:
        """An unusable source records the reason instead of raising.

        A camera is bound to a live-edited input field in the UI, so
        every half-typed string must be survivable.
        """
        cam = camera(source)
        assert cam.handler is None
        assert cam.source.error
        assert isinstance(cam.source, SourceInfo)

    def test_bad_source_refuses_to_start(self, camera: CameraFactory) -> None:
        """start() is where an unusable source becomes an error."""
        cam = camera("bogus://x")
        with pytest.raises(CameraError, match="cannot start"):
            cam.start()


class TestLifecycle:
    """Starting and stopping."""

    def test_start_opens_and_runs(self, camera: CameraFactory) -> None:
        """Starting marks the camera running and opens the source."""
        cam = camera("testimage://", max_framerate=10)
        cam.start()
        assert cam.running is True
        assert cam.handler is not None
        assert cam.handler.is_open

    def test_start_is_idempotent(self, camera: CameraFactory) -> None:
        """Starting twice does not spawn a second capture thread."""
        cam = camera("testimage://")
        cam.start()
        thread = cam._thread
        cam.start()
        assert cam._thread is thread

    def test_stop_releases(self, camera: CameraFactory) -> None:
        """Stopping closes the source and clears running."""
        cam = camera("testimage://")
        cam.start()
        cam.stop()
        assert cam.running is False
        assert cam.handler is not None
        assert not cam.handler.is_open

    def test_stop_without_start_is_harmless(self, camera: CameraFactory) -> None:
        """Stopping an idle camera does nothing."""
        camera("testimage://").stop()

    def test_restart(self, camera: CameraFactory) -> None:
        """A stopped camera can be started again."""
        cam = camera("testimage://")
        cam.start()
        cam.stop()
        cam.start()
        assert cam.running is True

    def test_background_thread_captures(self, camera: CameraFactory) -> None:
        """The capture thread fills in image without being driven."""
        cam = camera("testimage://", max_width=160, max_height=120, max_framerate=20)
        cam.start()
        assert wait_for(lambda: bool(cam.image))

    def test_no_thread_when_background_is_false(self, camera: CameraFactory) -> None:
        """Scripted use opens the device but captures nothing on its own."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)
        assert cam._thread is None
        assert cam.image == b""
        assert cam.read()

    def test_context_manager(self, camera: CameraFactory) -> None:
        """The camera starts on entry and stops on exit."""
        cam = camera("testimage://", max_width=160, max_height=120)
        with cam as entered:
            assert entered is cam
            assert cam.running is True
        assert cam.running is False


class TestRunningField:
    """Controlling the camera through its synced ``running`` field."""

    def test_setting_running_starts(self, camera: CameraFactory) -> None:
        """A peer writing running=True starts the capture."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.running = True
        assert cam._active is True
        assert wait_for(lambda: bool(cam.image))

    def test_clearing_running_stops(self, camera: CameraFactory) -> None:
        """A peer writing running=False stops the capture."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()
        cam.running = False
        assert cam._active is False
        assert cam._thread is None

    def test_failed_remote_start_resets_the_flag(self, camera: CameraFactory) -> None:
        """running does not stay true when the source cannot open."""
        cam = camera("bogus://x")
        cam.running = True
        assert cam.running is False


class TestStatus:
    """The status field: what the camera is doing, in words."""

    def test_starts_stopped(self, camera: CameraFactory) -> None:
        """A resolvable source that has not been started is just stopped."""
        assert camera("testimage://").status == STATUS_STOPPED

    def test_running(self, camera: CameraFactory) -> None:
        """Capturing says so."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()
        assert cam.status == STATUS_RUNNING

    def test_stopped_again(self, camera: CameraFactory) -> None:
        """Stopping clears any earlier complaint."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()
        cam.stop()
        assert cam.status == STATUS_STOPPED

    def test_reports_an_empty_source(self, camera: CameraFactory) -> None:
        """A camera with nowhere to look says so."""
        assert camera("").status == "no source configured"

    def test_reports_why_a_source_will_not_resolve(
        self, camera: CameraFactory
    ) -> None:
        """The resolution failure is carried in full."""
        cam = camera("testimage://nope")
        assert "Unknown test image" in cam.status

    def test_reports_why_a_device_will_not_open(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A source that resolves but will not open explains itself.

        Without this the UI could only say "stopped", since the source
        string itself was perfectly valid.
        """
        fake_capture(opened=False)
        cam = camera("v4l:///dev/video0")

        with pytest.raises(CameraError):
            cam.start()

        assert "Failed to open" in cam.status
        assert cam.source.error == ""

    def test_clears_once_a_source_works(self, camera: CameraFactory) -> None:
        """Fixing the source string clears the complaint."""
        cam = camera("testimage://nope")
        assert "Unknown test image" in cam.status

        cam.source_str = "testimage://pm5544"

        assert cam.status == STATUS_STOPPED

    def test_reports_a_failing_capture(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A camera that opened but cannot produce frames says why."""
        fake_capture(frames=[None])
        cam = camera("v4l:///dev/video0", max_framerate=30)
        cam.start()

        assert wait_for(lambda: "capture failing" in cam.status)

    def test_recovers_after_a_failing_capture(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """Once frames flow again the complaint goes away."""
        frame = np.full((480, 640, 3), 5, np.uint8)
        # Fails once, then recovers.
        fake_capture(frames=[None, frame])
        cam = camera("v4l:///dev/video0", max_framerate=30)
        cam.start()

        assert wait_for(lambda: cam.status == STATUS_RUNNING)

    def test_is_writable(self, camera: CameraFactory) -> None:
        """Status is a synced field like any other, not a read-only view."""
        cam = camera("testimage://")
        cam.status = "poked"
        assert cam.status == "poked"


class TestReading:
    """Producing frames."""

    def test_read_before_start(self, camera: CameraFactory) -> None:
        """Reading a stopped camera is an error, not an empty frame."""
        with pytest.raises(CameraNotStartedError):
            camera("testimage://").read()

    def test_read_frame_before_start(self, camera: CameraFactory) -> None:
        """The raw-frame path refuses just as firmly."""
        with pytest.raises(CameraNotStartedError):
            camera("testimage://").read_frame()

    def test_read_returns_encoded_bytes(self, camera: CameraFactory) -> None:
        """read() encodes to JPEG and publishes to image."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)

        data = cam.read()

        assert data.startswith(b"\xff\xd8")
        assert cam.image == data

    def test_read_frame_returns_array(self, camera: CameraFactory) -> None:
        """read_frame() hands back raw pixels for every source type."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)

        frame = cam.read_frame()

        assert isinstance(frame, np.ndarray)
        assert frame.shape[2] == 3

    def test_dropped_frame_keeps_the_last_image(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A momentary drop re-serves the previous frame."""
        good = np.full((480, 640, 3), 9, np.uint8)
        fake_capture(frames=[good, None])
        cam = camera("v4l:///dev/video0")
        cam.start(background=False)

        first = cam.read()
        second = cam.read()

        assert second == first

    def test_drop_with_no_previous_frame_raises(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """With nothing to fall back on, the caller is told."""
        fake_capture(frames=[None])
        cam = camera("v4l:///dev/video0")
        cam.start(background=False)

        with pytest.raises(FrameUnavailableError):
            cam.read()


class TestReceivedDimensions:
    """Reporting the frame that actually arrived."""

    def test_records_size_and_ratio(self, camera: CameraFactory) -> None:
        """received_* describes the real frame, not the request."""
        cam = camera("testimage://", max_width=1920, max_height=1080)
        cam.start(background=False)

        cam.read()

        # A 4:3 pattern fitted into a 16:9 request.
        assert (cam.received_width, cam.received_height) == (1440, 1080)
        assert cam.received_ratio == pytest.approx(4 / 3, abs=1e-3)

    def test_tracks_a_resolution_change(self, camera: CameraFactory) -> None:
        """Changing the bounds updates what is reported."""
        cam = camera("testimage://", max_width=320, max_height=240)
        cam.start(background=False)
        cam.read()

        cam.max_width, cam.max_height = 160, 120
        cam.read()

        assert (cam.received_width, cam.received_height) == (160, 120)

    def test_reports_device_size(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A device that ignores the request still reports honestly."""
        fake_capture(width=640, height=480)
        cam = camera("v4l:///dev/video0", max_width=1920, max_height=1080)
        cam.start(background=False)

        cam.read()

        assert (cam.received_width, cam.received_height) == (640, 480)


class TestEncoding:
    """Turning frames into bytes."""

    def test_reuses_the_encode_for_an_unchanged_frame(
        self, camera: CameraFactory
    ) -> None:
        """A static source is encoded once, not once per read."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)

        assert cam.read() is cam.read()

    def test_quality_change_re_encodes(self, camera: CameraFactory) -> None:
        """Lowering quality produces a smaller frame immediately."""
        cam = camera("testimage://", max_width=320, max_height=240, quality=95)
        cam.start(background=False)
        large = cam.read()

        cam.quality = 10
        small = cam.read()

        assert len(small) < len(large)

    def test_png_encoding(self, camera: CameraFactory) -> None:
        """The mimetype selects the container."""
        cam = camera("testimage://", max_width=160, max_height=120, mimetype="image/png")
        cam.start(background=False)

        assert cam.read().startswith(b"\x89PNG")

    def test_unsupported_mimetype(self, camera: CameraFactory) -> None:
        """An unencodable mimetype names what is supported."""
        cam = camera("testimage://", max_width=160, max_height=120, mimetype="image/gif")
        cam.start(background=False)

        with pytest.raises(CameraError, match="image/jpeg"):
            cam.read()

    def test_encoded_frame_decodes(self, camera: CameraFactory) -> None:
        """The published bytes are a real image."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)

        decoded = cv2.imdecode(np.frombuffer(cam.read(), np.uint8), cv2.IMREAD_COLOR)

        assert decoded is not None
        assert decoded.shape[:2] == (120, 160)


class TestLiveSettings:
    """Settings that take effect without a restart."""

    def test_capture_settings_snapshot(self, camera: CameraFactory) -> None:
        """The camera's bounds are passed to the source verbatim."""
        cam = camera("testimage://", max_width=800, max_height=600, max_framerate=24)
        settings = cam.capture_settings()
        assert (settings.max_width, settings.max_height, settings.max_framerate) == (
            800,
            600,
            24,
        )

    @pytest.mark.parametrize("value", [640.0, "640", 640])
    def test_coerces_bound_values(self, camera: CameraFactory, value: object) -> None:
        """UI number fields and remote peers may send floats or strings."""
        cam = camera("testimage://")
        cam.max_width = value
        assert cam.capture_settings().max_width == 640

    @pytest.mark.parametrize("value", [None, 0, -5, "nonsense"])
    def test_rejects_unusable_bounds(self, camera: CameraFactory, value: object) -> None:
        """A cleared or nonsensical field never yields a zero dimension."""
        cam = camera("testimage://")
        cam.max_width = value
        assert cam.capture_settings().max_width >= 1

    def test_resolution_change_applies_to_next_frame(
        self, camera: CameraFactory
    ) -> None:
        """No restart is needed to re-render at a new size."""
        cam = camera("testimage://", max_width=320, max_height=240)
        cam.start(background=False)
        cam.read()

        cam.max_width, cam.max_height = 160, 120

        assert cam.read_frame().shape[:2] == (120, 160)  # type: ignore[union-attr]

    def test_framerate_change_applies_to_the_loop(self, camera: CameraFactory) -> None:
        """The capture loop re-reads its interval every iteration."""
        cam = camera("testimage://", max_width=160, max_height=120, max_framerate=1)
        cam.start()
        cam.max_framerate = 30
        # At the original 1fps this would take a second per frame.
        assert wait_for(lambda: bool(cam.image), timeout=2.0)


class TestRetargeting:
    """Changing the source string on a live camera."""

    def test_retarget_while_stopped(self, camera: CameraFactory) -> None:
        """A new source is resolved but not started."""
        cam = camera("testimage://")

        cam.source_str = "v4l:///dev/video0"

        assert cam.source.scheme == "v4l"
        assert cam.source.handler == "V4LSource"
        assert cam.running is False

    def test_retarget_while_running_restarts(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A running camera follows its source string."""
        fake_capture()
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()

        cam.source_str = "v4l:///dev/video0"

        assert cam.running is True
        assert cam.source.handler == "V4LSource"

    def test_retarget_to_bad_source_stops(self, camera: CameraFactory) -> None:
        """An unusable new source stops the camera and records why."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()

        cam.source_str = "testimage://nope"

        assert cam.running is False
        assert cam.handler is None
        assert "Unknown test image" in cam.source.error

    def test_recovers_from_a_bad_source(self, camera: CameraFactory) -> None:
        """Typing a valid source after a bad one clears the error."""
        cam = camera("bogus://x")

        cam.source_str = "testimage://"

        assert cam.source.error == ""
        assert cam.handler is not None
        cam.start()
        assert cam.running is True

    def test_resumes_once_a_typed_source_works(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """A source typed a character at a time picks itself back up.

        Reproduces typing "/dev/video10" into the UI: the intermediate
        "/dev/vi" resolves, because the V4L handler claims any /dev path,
        but fails to open. The camera must resume on its own once the
        full path arrives rather than waiting for a manual start.
        """
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()
        assert cam.running is True

        fake_capture(opened=False)
        cam.source_str = "/dev/vi"
        assert cam.running is False

        fake_capture(opened=True)
        cam.source_str = "/dev/video10"

        assert cam.running is True
        assert cam.source.target == "/dev/video10"

    def test_resumes_after_an_unresolvable_source(
        self, camera: CameraFactory
    ) -> None:
        """The same holds when the half-typed string does not resolve."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()

        cam.source_str = "testimag"
        assert cam.running is False
        assert cam.handler is None

        cam.source_str = "testimage://pm5544"

        assert cam.running is True

    def test_does_not_resume_when_stopped_by_hand(
        self, camera: CameraFactory
    ) -> None:
        """Editing the source of a stopped camera leaves it stopped."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start()
        cam.stop()

        cam.source_str = "testimage://pm5544"

        assert cam.running is False

    def test_never_started_does_not_resume(self, camera: CameraFactory) -> None:
        """A camera that was never started stays stopped when retargeted."""
        cam = camera("testimage://", max_width=160, max_height=120)

        cam.source_str = "testimage://pm5544"

        assert cam.running is False

    def test_resumes_without_a_thread_when_scripted(
        self, camera: CameraFactory
    ) -> None:
        """A camera started with background=False resumes the same way."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)

        cam.source_str = "testimage://pm5544"

        assert cam.running is True
        assert cam._thread is None

    def test_source_info_identity_is_stable(self, camera: CameraFactory) -> None:
        """Retargeting mutates the description rather than replacing it.

        UI bindings and sync subscriptions hold a reference to this
        object, so it must outlive a source change.
        """
        cam = camera("testimage://")
        info = cam.source

        cam.source_str = "v4l:///dev/video0"

        assert cam.source is info
        assert info.scheme == "v4l"


class TestCapabilities:
    """Reporting what the source can do."""

    def test_device_capabilities_are_copied(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        """max_supported_* comes from the source at open time."""
        fake_capture(width=1920, height=1080, framerate=60)
        cam = camera("v4l:///dev/video0")

        cam.start(background=False)

        assert cam.max_supported_width == 1920
        assert cam.max_supported_height == 1080
        assert cam.max_supported_framerate == 60

    def test_unbounded_source_reports_none(self, camera: CameraFactory) -> None:
        """A rendered source advertises no ceiling."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)
        assert cam.max_supported_width is None

    def test_describe_capabilities(self, camera: CameraFactory) -> None:
        """The human-readable summary covers both cases."""
        cam = camera("testimage://", max_width=160, max_height=120)
        cam.start(background=False)
        assert cam.describe_capabilities() == "any size @ any rate"

        cam.max_supported_width, cam.max_supported_height = 640, 480
        cam.max_supported_framerate = 30
        assert cam.describe_capabilities() == "640x480 @ 30fps"

    def test_capability_fields_are_writable(self, camera: CameraFactory) -> None:
        """Nothing in SpiriSynq is read-only, including reported values."""
        cam = camera("testimage://")
        cam.max_supported_width = 4096
        assert cam.max_supported_width == 4096

    def test_identity_survives_when_source_reports_none(
        self,
        camera: CameraFactory,
        fake_capture: Callable[..., CaptureHolder],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A hand-set vendor is not clobbered by a silent source."""
        # An empty sysfs root means the source can report no identity.
        monkeypatch.setattr("SpiriCamera.sources.v4l._SYSFS_ROOT", tmp_path)
        fake_capture()
        cam = camera("v4l:///dev/video0")
        cam.vendor = "hand written"

        cam.start(background=False)

        assert cam.vendor == "hand written"
