"""Tests for projecting points in the world onto a camera's picture."""

from __future__ import annotations

import math

import pytest
from SpiriSynq.example_types.position import Accuracy, Offset, Orientation, Pose

from SpiriCamera.projection import project
from SpiriCamera.sources import Intrinsics

LAT, LON, HEIGHT = 45.0, -75.0, 100.0

#: 90° across a 640x480 frame: fx = 320, so a point 10 m off at 100 m is 32 px off.
LENS = Intrinsics.from_fov(640, 480, 90.0)


def camera(yaw: float = 0.0, pitch: float = 0.0) -> Pose:
    """A camera at LAT/LON/HEIGHT, level and facing north unless turned (degrees)."""
    body = Orientation.from_euler(0.0, math.radians(pitch), math.radians(yaw))
    return Pose(
        offset=Offset.from_geodetic(LAT, LON, HEIGHT),
        orientation=Orientation.ned_to_ecef(LAT, LON) * body,
    )


def ned(
    north: float, east: float, down: float, accuracy: Accuracy | None = None
) -> Pose:
    """A point *north*, *east* and *down* metres from the camera."""
    local = Offset(north, east, down).rotate_by(Orientation.ned_to_ecef(LAT, LON))
    return Pose(
        offset=Offset.from_geodetic(LAT, LON, HEIGHT) + local,
        orientation=None,
        accuracy=accuracy,
    )


class TestProject:
    def test_a_point_ahead_is_at_the_principal_point(self) -> None:
        view = project(camera(), LENS, ned(100, 0, 0), 640, 480)

        assert view is not None
        assert view.x == pytest.approx(320)
        assert view.y == pytest.approx(240)
        assert view.range == pytest.approx(100)

    def test_right_is_right_and_down_is_down(self) -> None:
        """The camera's frame is FRD: +y right of the picture, +z down it."""
        view = project(camera(), LENS, ned(100, 10, 10), 640, 480)

        assert view is not None
        assert view.x == pytest.approx(320 + 32)
        assert view.y == pytest.approx(240 + 32)

    def test_follows_the_camera_turning(self) -> None:
        """Turned east, a point to the east is dead ahead."""
        view = project(camera(yaw=90), LENS, ned(0, 100, 0), 640, 480)

        assert view is not None
        assert (view.x, view.y) == (pytest.approx(320), pytest.approx(240))

    def test_follows_the_camera_pitching(self) -> None:
        """Pitched down 45°, a point 100 m ahead and 100 m below is centred."""
        view = project(camera(pitch=-45), LENS, ned(100, 0, 100), 640, 480)

        assert view is not None
        assert (view.x, view.y) == (pytest.approx(320), pytest.approx(240))

    def test_scales_the_lens_to_the_frame(self) -> None:
        view = project(camera(), LENS, ned(100, 10, 0), 320, 240)

        assert view is not None
        assert view.x == pytest.approx(160 + 16)

    def test_behind_the_camera_is_nothing(self) -> None:
        assert project(camera(), LENS, ned(-100, 0, 0), 640, 480) is None

    def test_a_camera_without_axes_sees_nothing(self) -> None:
        point_camera = Pose(offset=camera().offset, orientation=None)

        assert project(point_camera, LENS, ned(100, 0, 0), 640, 480) is None

    def test_off_frame_is_still_placed(self) -> None:
        """In front but out of shot: a pixel off the frame, not None."""
        view = project(camera(), LENS, ned(100, 200, 0), 640, 480)

        assert view is not None
        assert view.x > 640
        assert not view.visible(640, 480)


class TestRadius:
    def test_is_the_accuracy_seen_from_here(self) -> None:
        """±10 m level at 100 m ahead is ±32 px across the picture."""
        view = project(camera(), LENS, ned(100, 0, 0, Accuracy(10.0, 0.0)), 640, 480)

        assert view is not None
        assert view.r == pytest.approx(32, rel=0.05)

    def test_includes_the_vertical_bound(self) -> None:
        level = project(camera(), LENS, ned(100, 0, 0, Accuracy(1.0, 0.0)), 640, 480)
        tall = project(camera(), LENS, ned(100, 0, 0, Accuracy(1.0, 10.0)), 640, 480)

        assert level is not None and tall is not None
        assert tall.r == pytest.approx(32, rel=0.05)
        assert tall.r > level.r  # type: ignore[operator]

    def test_unknown_accuracy_has_no_radius(self) -> None:
        view = project(camera(), LENS, ned(100, 0, 0), 640, 480)

        assert view is not None
        assert view.r is None

    def test_a_cylinder_reaching_behind_the_camera_has_no_radius(self) -> None:
        view = project(camera(), LENS, ned(5, 0, 0, Accuracy(10.0, 1.0)), 640, 480)

        assert view is not None
        assert view.r is None


class TestEdge:
    def test_a_circle_across_the_edge_is_on_the_edge(self) -> None:
        # 40 px from the right edge, with a ~64 px circle.
        view = project(camera(), LENS, ned(100, 87.5, 0, Accuracy(20.0, 0.0)), 640, 480)

        assert view is not None
        assert view.edge
        assert view.visible(640, 480)

    def test_well_inside_is_not(self) -> None:
        view = project(camera(), LENS, ned(100, 0, 0, Accuracy(1.0, 1.0)), 640, 480)

        assert view is not None
        assert not view.edge

    def test_wholly_outside_is_not_on_the_edge(self) -> None:
        view = project(camera(), LENS, ned(100, 300, 0), 640, 480)

        assert view is not None
        assert not view.edge
