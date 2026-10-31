"""Tests for a camera's lens: the Intrinsics model and where it comes from."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from SpiriCamera.camera import Camera
from SpiriCamera.main import Settings
from SpiriCamera.sources.base import Intrinsics, SourceCapabilities

from .conftest import CaptureHolder

CameraFactory = Callable[..., Camera]

# The simulated gimbal camera's camera_info: horizontal_fov 2.0 rad at 640x480.
GAZEBO = Intrinsics(640, 480, 205.4696, 205.4696, 320, 240)


class TestIntrinsics:
    """The pinhole model on its own."""

    def test_field_of_view(self) -> None:
        assert GAZEBO.hfov == pytest.approx(114.59, abs=0.01)
        assert GAZEBO.vfov == pytest.approx(98.86, abs=0.01)

    def test_from_fov_round_trips(self) -> None:
        lens = Intrinsics.from_fov(1920, 1080, 90, 60)
        assert lens.fx == pytest.approx(960)
        assert (lens.cx, lens.cy) == (960, 540)
        assert lens.hfov == pytest.approx(90)
        assert lens.vfov == pytest.approx(60)

    def test_from_fov_without_vfov_assumes_square_pixels(self) -> None:
        lens = Intrinsics.from_fov(640, 480, 114.59156)
        assert lens.fy == lens.fx
        assert lens.vfov == pytest.approx(GAZEBO.vfov, abs=0.01)

    def test_scaled_keeps_the_field_of_view(self) -> None:
        half = GAZEBO.scaled(320, 240)
        assert (half.fx, half.cx, half.cy) == pytest.approx((102.7348, 160, 120))
        assert half.hfov == pytest.approx(GAZEBO.hfov)

    @pytest.mark.parametrize(
        "make",
        [
            lambda: Intrinsics(0, 480, 200, 200, 0, 0),
            lambda: Intrinsics(640, 480, 0, 200, 0, 0),
            lambda: Intrinsics.from_fov(640, 480, 180),
            lambda: Intrinsics.from_fov(640, 480, 0),
        ],
    )
    def test_refuses_what_isnt_a_lens(self, make: Callable[[], Intrinsics]) -> None:
        with pytest.raises(ValueError):
            make()


class TestOrigins:
    """Picking the camera's lens from what each origin reported."""

    def test_unknown_until_reported(self, camera: CameraFactory) -> None:
        cam = camera()
        cam.start(background=False)
        cam.read()
        assert cam.intrinsics is None

    def test_full_model(self, camera: CameraFactory) -> None:
        cam = camera()
        cam.set_intrinsics("mavlink", width=640, height=480, fx=205.4696)
        assert cam.intrinsics == Intrinsics(
            640, 480, 205.4696, 205.4696, 320, 240, "mavlink"
        )

    def test_fov_waits_for_a_frame_size(self, camera: CameraFactory) -> None:
        cam = camera(max_width=320, max_height=240)
        cam.set_intrinsics("manual", hfov=90)
        assert cam.intrinsics is None

        cam.start(background=False)
        cam.read()

        assert cam.intrinsics is not None
        assert (cam.intrinsics.width, cam.intrinsics.height) == (320, 240)
        assert cam.intrinsics.hfov == pytest.approx(90)
        assert cam.intrinsics.origin == "manual"

    def test_fov_uses_the_native_size(
        self, camera: CameraFactory, fake_capture: Callable[..., CaptureHolder]
    ) -> None:
        fake_capture(width=1920, height=1080)
        cam = camera("v4l:///dev/video0", max_width=640, max_height=360)
        cam.set_intrinsics("mavlink", hfov=90, vfov=60)
        cam.start(background=False)
        assert cam.intrinsics is not None
        assert (cam.intrinsics.width, cam.intrinsics.height) == (1920, 1080)

    def test_precedence(self, camera: CameraFactory) -> None:
        cam = camera()
        cam.set_intrinsics("mavlink", width=640, height=480, fx=100)
        cam.set_intrinsics("manual", width=640, height=480, fx=300)
        assert cam.intrinsics is not None and cam.intrinsics.origin == "manual"

        cam.set_intrinsics("mavlink", width=640, height=480, fx=200)
        assert cam.intrinsics.fx == 300

        cam.clear_intrinsics("manual")
        assert cam.intrinsics is not None and cam.intrinsics.fx == 200

        cam.clear_intrinsics("mavlink")
        assert cam.intrinsics is None

    def test_source_report(self, camera: CameraFactory) -> None:
        cam = camera()
        cam.set_intrinsics("mavlink", width=640, height=480, fx=100)
        cam._apply(SourceCapabilities(intrinsics=GAZEBO))
        assert cam.intrinsics is not None
        assert cam.intrinsics.origin == "source"
        assert cam.intrinsics.fx == GAZEBO.fx

        cam.set_intrinsics("manual", hfov=60)
        assert cam.intrinsics is not None and cam.intrinsics.origin == "manual"

    def test_refuses_unknown_origins_and_bad_numbers(
        self, camera: CameraFactory
    ) -> None:
        cam = camera()
        with pytest.raises(ValueError):
            cam.set_intrinsics("guess", hfov=60)
        with pytest.raises(ValueError):
            cam.set_intrinsics("manual", hfov=200)
        with pytest.raises(ValueError):
            cam.set_intrinsics("manual", fx=200)  # no size
        assert cam.intrinsics is None

    def test_survives_a_rehydrate(
        self, camera: CameraFactory, synq_session: object
    ) -> None:
        cam = camera()
        cam.sync()
        cam.set_intrinsics("manual", width=640, height=480, fx=205.4696)

        restored = synq_session.type_registry.load(cam.sync_dumps())  # type: ignore[attr-defined]

        assert restored.intrinsics == cam.intrinsics


class TestSettings:
    """The lens as SPIRICAMERA_ environment variables."""

    def test_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert Settings().intrinsics() is None

    def test_fov(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPIRICAMERA_HFOV", "114.6")
        assert Settings().intrinsics() == {"hfov": 114.6, "vfov": 0.0}

    def test_calibration_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPIRICAMERA_HFOV", "114.6")
        monkeypatch.setenv("SPIRICAMERA_FX", "205.47")
        monkeypatch.setenv("SPIRICAMERA_CALIBRATION_WIDTH", "640")
        monkeypatch.setenv("SPIRICAMERA_CALIBRATION_HEIGHT", "480")
        assert Settings().intrinsics() == {
            "width": 640,
            "height": 480,
            "fx": 205.47,
            "fy": 0.0,
            "cx": 0.0,
            "cy": 0.0,
        }
