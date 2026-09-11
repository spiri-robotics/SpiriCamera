"""Tests for the NiceGUI test UI's server-side pieces."""

from __future__ import annotations

import time

import pytest
from nicegui.testing.user import User

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
