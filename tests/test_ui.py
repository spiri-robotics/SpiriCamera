"""Tests for the NiceGUI test UI's server-side pieces."""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest
from nicegui.testing.user import User

from SpiriCamera import exif
from SpiriCamera import ui as camera_ui
from SpiriCamera.camera import Camera


class TestRateMeter:
    """The bandwidth meter behind the readout under the image."""

    def test_starts_empty(self) -> None:
        """Nothing served means no rate, not a division by zero."""
        assert camera_ui.RateMeter().rates() == (0.0, 0.0)

    def test_single_sample_is_not_a_rate(self) -> None:
        """One frame gives no interval to measure over."""
        meter = camera_ui.RateMeter()
        meter.record(1000)
        assert meter.rates() == (0.0, 0.0)

    def test_measures_frames_and_bytes(self) -> None:
        """Both rates reflect what was recorded."""
        meter = camera_ui.RateMeter(window=5.0)
        for _ in range(10):
            meter.record(1000)
            time.sleep(0.01)

        frames_per_second, bytes_per_second = meter.rates()

        assert frames_per_second > 0
        assert bytes_per_second == pytest.approx(frames_per_second * 1000, rel=0.01)

    def test_forgets_old_samples(self) -> None:
        """The window rolls, so a stopped stream decays to zero."""
        meter = camera_ui.RateMeter(window=0.05)
        meter.record(1000)
        meter.record(1000)
        time.sleep(0.1)
        assert meter.rates() == (0.0, 0.0)


class TestFrameRoute:
    """The HTTP route the browser pulls frames from."""

    @pytest.fixture
    def served_camera(self, monkeypatch: pytest.MonkeyPatch) -> Camera:
        """Install a known camera as the process-wide one."""
        cam = Camera(
            "testimage://", max_width=160, max_height=120, synq_auto_start=False
        )
        monkeypatch.setattr(camera_ui, "_camera", cam)
        yield cam
        cam.stop()

    def test_placeholder_before_the_first_frame(self, served_camera: Camera) -> None:
        """A stopped camera serves a placeholder, not an error."""
        response = camera_ui.serve_frame()
        assert response.status_code == 200
        assert response.media_type == "image/png"

    def test_serves_the_current_frame(self, served_camera: Camera) -> None:
        """Once running, the route hands out the latest encoded frame."""
        served_camera.start(background=False)
        served_camera.read()

        response = camera_ui.serve_frame()

        assert response.body == served_camera.image
        assert response.media_type == "image/jpeg"

    def test_frames_are_not_cached(self, served_camera: Camera) -> None:
        """A stale frame must never be reused by a proxy."""
        served_camera.start(background=False)
        served_camera.read()

        assert camera_ui.serve_frame().headers["Cache-Control"] == "no-store"

    def test_serving_feeds_the_meter(self, served_camera: Camera) -> None:
        """The readout measures real bytes leaving the route."""
        served_camera.start(background=False)
        served_camera.read()
        meter = camera_ui.RateMeter()
        monkey = camera_ui.frame_meter
        camera_ui.frame_meter = meter
        try:
            camera_ui.serve_frame()
            camera_ui.serve_frame()
        finally:
            camera_ui.frame_meter = monkey

        frames_per_second, _ = meter.rates()
        assert frames_per_second > 0


class TestSharedCamera:
    """One camera per process, not one per page load."""

    def test_returns_the_same_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeated calls share a camera, so the synq topic is unique."""
        cam = Camera("testimage://", synq_auto_start=False)
        monkeypatch.setattr(camera_ui, "_camera", cam)

        assert camera_ui.get_camera() is cam
        assert camera_ui.get_camera() is camera_ui.get_camera()


class TestPage:
    """Building the page itself."""

    async def test_page_builds(self, user: User) -> None:
        """Every binding resolves against the current camera fields.

        This is the regression guard for binding a control to a field
        that no longer exists: NiceGUI raises at build time, so simply
        rendering the page is the test.
        """
        await user.open("/")
        await user.should_see("Start")
        await user.should_see("Resolved Source")

    async def test_shows_the_resolved_source(self, user: User) -> None:
        """The source panel reflects the camera's parsed source."""
        await user.open("/")
        await user.should_see("testimage")

    async def test_shows_the_bandwidth_readout(self, user: User) -> None:
        """The meter renders even before anything has been served."""
        await user.open("/")
        await user.should_see("idle")


class TestTagDisplay:
    """Rendering the tags read off the current frame."""

    def test_untagged_says_so(self) -> None:
        """An empty display would read as a broken binding."""
        assert camera_ui._format_tags({}) == "untagged"

    def test_one_tag_per_line_sorted(self) -> None:
        """Sorted, so a tag does not move around between frames."""
        rendered = camera_ui._format_tags({"zulu": "1", "alpha": "2"})

        assert rendered == "alpha: 2\nzulu: 1"


class TestExtraTagEntry:
    """Parsing the Extra Tags field onto the camera."""

    @pytest.fixture
    def cam(self) -> Camera:
        """A camera that touches neither a device nor the network."""
        return Camera("testimage://", synq_auto_start=False)

    def test_parses_pairs(self, cam: Camera) -> None:
        """Comma-separated name=value, whitespace forgiven."""
        camera_ui._apply_extra_tags(cam, " mission=probe-1, operator = alex ")

        assert cam.exif_extra == {"mission": "probe-1", "operator": "alex"}

    def test_ignores_a_fragment_without_a_separator(self, cam: Camera) -> None:
        """Typing is debounced, not atomic; half an entry is not a tag."""
        camera_ui._apply_extra_tags(cam, "mission=probe-1, operat")

        assert cam.exif_extra == {"mission": "probe-1"}

    def test_empty_field_clears(self, cam: Camera) -> None:
        """Deleting the text is how the tags are removed."""
        camera_ui._apply_extra_tags(cam, "mission=probe-1")
        camera_ui._apply_extra_tags(cam, "")

        assert cam.exif_extra == {}

    def test_an_empty_value_is_kept(self, cam: Camera) -> None:
        """``name=`` is a tag being typed, not a tag being dropped."""
        camera_ui._apply_extra_tags(cam, "mission=")

        assert cam.exif_extra == {"mission": ""}


class TestFrameAge:
    """Capture-to-serve age, measured where frames leave."""

    def test_untagged_frames_have_no_knowable_age(self) -> None:
        """Absent a timestamp, zero means unknown, not instantaneous."""
        jpeg = cv2.imencode(".jpg", np.zeros((4, 4, 3), np.uint8))[1].tobytes()

        assert camera_ui._frame_age(jpeg) == 0.0

    def test_measured_from_the_frame(self) -> None:
        """The age is whatever the frame's own EXIF says it is."""
        jpeg = cv2.imencode(".jpg", np.zeros((4, 4, 3), np.uint8))[1].tobytes()
        tagged = exif.embed(jpeg, {"timestamp": f"{time.time() - 0.25:.6f}"})

        assert camera_ui._frame_age(tagged) == pytest.approx(0.25, abs=0.05)

    def test_a_clock_ahead_of_ours_is_clamped(self) -> None:
        """A remote camera must not report a frame from the future."""
        jpeg = cv2.imencode(".jpg", np.zeros((4, 4, 3), np.uint8))[1].tobytes()
        tagged = exif.embed(jpeg, {"timestamp": f"{time.time() + 60:.6f}"})

        assert camera_ui._frame_age(tagged) == 0.0

    def test_an_unreadable_timestamp_is_not_fatal(self) -> None:
        """A tag is free-form text and may hold anything at all."""
        jpeg = cv2.imencode(".jpg", np.zeros((4, 4, 3), np.uint8))[1].tobytes()
        tagged = exif.embed(jpeg, {"timestamp": "soon"})

        assert camera_ui._frame_age(tagged) == 0.0

    def test_the_meter_averages_rather_than_samples(self) -> None:
        """The reported age must not depend on when it is read.

        A frame's age sweeps across a whole frame interval while it waits
        to be fetched, so a single reading beats against the capture loop
        and slides instead of settling.
        """
        meter = camera_ui.RateMeter(window=5.0)
        for age in (0.010, 0.020, 0.030):
            meter.record(1000, age)

        assert meter.mean_age() == pytest.approx(0.020)

    def test_untagged_frames_do_not_drag_the_mean_down(self) -> None:
        """An unknown age is excluded, not counted as zero."""
        meter = camera_ui.RateMeter(window=5.0)
        meter.record(1000, 0.020)
        meter.record(1000)

        assert meter.mean_age() == pytest.approx(0.020)

    def test_nothing_served_has_no_age(self) -> None:
        """No frames means no reading, not a division by zero."""
        assert camera_ui.RateMeter().mean_age() == 0.0

    async def test_shown_once_a_frame_has_been_served(self, user: User) -> None:
        """The readout appears alongside the bandwidth figures."""
        cam = camera_ui.get_camera()
        cam.start(background=False)
        cam.read()
        camera_ui.serve_frame()
        camera_ui.serve_frame()

        await user.open("/")

        await user.should_see("ms old")
