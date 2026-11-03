"""Where a point in the world lands in a camera's picture.

The overlay uses this to put a widget on an object in SpiriSynq's frame
tree (see :ref:`world-overlays`), so a widget's template never does the
geometry itself; it's plain functions on poses, usable on their own.

A camera's own frame is FRD, like the rest of the frame tree: ``x`` along
the optical axis, out of the lens; ``y`` to the right of the picture;
``z`` down it.  So with the camera's :py:class:`~SpiriSynq.example_types.position.Pose`
(in ECEF, like every root-frame pose) and its
:py:class:`~SpiriCamera.sources.Intrinsics`, a point at ``(x, y, z)`` in
the camera's frame is at pixel ``(cx + fx * y / x, cy + fy * z / x)``.
A camera on a gimbal with an identity mount looks along the gimbal's
``x`` axis, as a rangefinder on the same gimbal does.

The lens is the pinhole model, with no distortion: a wide lens draws a
point near its edge some pixels off from where this puts it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from SpiriSynq.example_types.position import Offset, Orientation, Pose

from SpiriCamera.sources import Intrinsics

#: Nearest a point is drawn, in metres along the optical axis: anything
#: closer is treated as behind the camera.
NEAR = 0.01

#: Points around the 90% cylinder whose projections bound its circle.
_RING = 16


@dataclass(frozen=True)
class View:
    """A point as a camera sees it.

    Attributes
    ----------
    x, y : float
        Its pixel, in the frame :py:func:`project` was given.  Off the
        frame when the point is in front of the camera but out of shot.
    r : float | None
        The radius in pixels of the point's 90% accuracy (its
        :py:class:`~SpiriSynq.example_types.position.Accuracy` cylinder)
        as seen from here: the circle around ``(x, y)`` that holds the
        cylinder's outline.  ``None`` when the point's accuracy is
        unknown, or when the cylinder reaches behind the camera.  It is
        the point's own uncertainty, not the camera's: see
        :ref:`world-overlays` for what it does and doesn't mean.
    range : float
        Metres from the camera to the point.
    edge : bool
        Whether the point, or its circle, crosses the frame's edge.
    """

    x: float
    y: float
    r: float | None
    range: float
    edge: bool

    def visible(self, width: int, height: int) -> bool:
        """Whether any of the point's circle is on a ``width`` x ``height`` frame."""
        r = self.r or 0.0
        return -r <= self.x <= width + r and -r <= self.y <= height + r


def to_camera(camera: Pose, point: Offset) -> Offset | None:
    """An ECEF *point* in *camera*'s own frame, or None if *camera* has no axes."""
    if camera.orientation is None:
        return None
    q = camera.orientation
    return (point - camera.offset).rotate_by(Orientation(q.w, -q.x, -q.y, -q.z))


def pixel(lens: Intrinsics, local: Offset) -> tuple[float, float] | None:
    """A point in the camera's frame as a pixel of *lens*, or None behind it."""
    if local.x < NEAR:
        return None
    return lens.cx + lens.fx * local.y / local.x, lens.cy + lens.fy * local.z / local.x


def project(camera: Pose, lens: Intrinsics, point: Pose, width: int, height: int) -> View | None:
    """Where *point* is in a ``width`` x ``height`` frame from *camera*.

    *camera* and *point* are root-frame (ECEF) poses, *lens* the camera's
    intrinsics at any size (scaled to the frame here).  None when the
    point is behind the camera, or the camera's pose is a point with no
    orientation to look along.
    """
    lens = lens.scaled(width, height)
    local = to_camera(camera, point.offset)
    if local is None:
        return None
    centre = pixel(lens, local)
    if centre is None:
        return None
    x, y = centre
    r = _radius(camera, lens, point, centre)
    view = View(x, y, r, math.sqrt(local.x**2 + local.y**2 + local.z**2), edge=False)
    rr = r or 0.0
    inside = rr <= x <= width - rr and rr <= y <= height - rr
    return replace(view, edge=view.visible(width, height) and not inside)


def _radius(
    camera: Pose, lens: Intrinsics, point: Pose, centre: tuple[float, float]
) -> float | None:
    """The radius around *centre* holding *point*'s accuracy cylinder."""
    accuracy = point.accuracy
    if accuracy is None or accuracy.horizontal is None:
        return None
    east, north, up = _level_axes(point.offset)
    h, v = accuracy.horizontal, accuracy.vertical
    heights = (-v, v) if v else (0.0,)
    r = 0.0
    for i in range(_RING):
        a = 2 * math.pi * i / _RING
        across = _scaled(east, h * math.cos(a)) + _scaled(north, h * math.sin(a))
        for dz in heights:
            local = to_camera(camera, point.offset + across + _scaled(up, dz))
            seen = pixel(lens, local) if local is not None else None
            if seen is None:
                return None
            r = max(r, math.hypot(seen[0] - centre[0], seen[1] - centre[1]))
    return r


def _level_axes(point: Offset) -> tuple[Offset, Offset, Offset]:
    """East, north and up at an ECEF *point*, up being the ellipsoid's normal."""
    lat, lon, _ = point.to_geodetic()
    phi, lam = math.radians(lat), math.radians(lon)
    east = Offset(-math.sin(lam), math.cos(lam), 0.0)
    north = Offset(-math.sin(phi) * math.cos(lam), -math.sin(phi) * math.sin(lam), math.cos(phi))
    up = Offset(math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))
    return east, north, up


def _scaled(axis: Offset, length: float) -> Offset:
    return Offset(axis.x * length, axis.y * length, axis.z * length)


__all__ = ["NEAR", "View", "pixel", "project", "to_camera"]
