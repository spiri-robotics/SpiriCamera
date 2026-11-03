"""Widgets placed by where objects are in the world: ``anchor`` and ``project``.

A camera placed in ECEF, level and facing north, with a 90° lens, draws
widgets on Positions published on the suite's isolated session, so this
goes through the real mirroring path, as a widget's objects do.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from SpiriSynq.example_types.position import (
    Accuracy,
    Offset,
    Orientation,
    Pose,
    Position,
    RootFrame,
)
from SpiriSynq.link import Link
from SpiriSynq.syncable_objects import SyncableObject

from SpiriCamera import overlay
from SpiriCamera.camera import Camera
from SpiriCamera.overlay import HudWidget, OverlayError

CameraFactory = Callable[..., Camera]

LAT, LON, HEIGHT = 45.0, -75.0, 100.0
SQUARE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
    '<rect width="20" height="20" fill="red"/></svg>'
)


def ned(north: float, east: float, down: float) -> Pose:
    """A point *north*, *east* and *down* metres from the camera."""
    local = Offset(north, east, down).rotate_by(Orientation.ned_to_ecef(LAT, LON))
    return Pose(
        offset=Offset.from_geodetic(LAT, LON, HEIGHT) + local,
        orientation=None,
        accuracy=Accuracy(1.0, 1.0),
    )


@dataclass
class Holder(SyncableObject):
    """Something linking to other objects, as a robot does to its gimbals."""

    targets: tuple[Link, ...] = ()


def wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting for condition")
        time.sleep(0.02)


@pytest.fixture
def published() -> Iterator[Callable[..., SyncableObject]]:
    """Publish objects on the test session, closing them afterwards."""
    made: list[SyncableObject] = []

    def publish(cls: type, **fields: object) -> SyncableObject:
        obj = cls(synq_topic=f"world/{uuid.uuid4().hex[:8]}", synq_authoritive=True, **fields)
        made.append(obj)
        return obj

    yield publish
    for obj in made:
        obj.close()


@pytest.fixture
def target(published: Callable[..., SyncableObject]) -> Callable[[Pose], Position]:
    """A Position at a fixed ECEF pose."""
    return lambda pose: published(Position, relative_to=RootFrame("ECEF"), mount=pose)  # type: ignore[return-value]


@pytest.fixture
def view_from(camera: CameraFactory) -> Callable[[HudWidget], Camera]:
    """A started 160x120 camera at LAT/LON/HEIGHT, facing north, showing *widget*.

    Rendered client-side, so a test reads where each copy went as markup.
    """

    def build(widget: HudWidget) -> Camera:
        cam = camera(
            "testimage://",
            synq_topic=f"cam_{uuid.uuid4().hex[:8]}",
            max_width=160,
            max_height=120,
            overlay_widgets={widget.synq_absolute_path: {}},
            overlay_client_render=True,
            synq_auto_start=True,
        )
        cam.relative_to = RootFrame("ECEF")
        cam.mount = Pose(
            offset=Offset.from_geodetic(LAT, LON, HEIGHT),
            orientation=Orientation.ned_to_ecef(LAT, LON),
        )
        cam.set_intrinsics("manual", hfov=90.0)
        cam.start(background=False)
        cam.read()
        return cam

    return build


def widget(published: Callable[..., SyncableObject], template: str) -> HudWidget:
    return published(HudWidget, svg_template=template)  # type: ignore[return-value]


def markup_of(cam: Camera, expected: str) -> str:
    """The client-side markup, once it has *expected* in it."""
    found = ""

    def rendered() -> bool:
        nonlocal found
        found = cam.render_overlays_for_client(160, 120)
        return expected in found

    wait_for(rendered)
    return found


class TestWorldPlacement:
    """Parsing ``anchor`` and ``project`` declarations."""

    def test_none_is_the_usual_widget(self) -> None:
        assert overlay.world_placement(SQUARE) == overlay.WorldPlacement()

    def test_anchor(self) -> None:
        template = "{# object: t = a/b #}{# anchor: t #}"
        assert overlay.world_placement(template) == overlay.WorldPlacement(anchor="t")

    def test_anchor_each(self) -> None:
        template = "{# object: robot = a/b #}{# anchor: each robot.gimbals #}"
        assert overlay.world_placement(template) == overlay.WorldPlacement(
            anchor="robot", each="gimbals"
        )

    def test_project(self) -> None:
        template = "{# object: a = x/a #}{# object: b = x/b #}{# project: a, b #}"
        assert overlay.world_placement(template).project == ("a", "b")

    @pytest.mark.parametrize(
        "template",
        [
            "{# object: t = a/b #}{# anchor: t #}{# anchor: t #}",
            "{# object: t = a/b #}{# anchor: t #}{# project: t #}",
            "{# object: t = a/b #}{# anchor: each t #}",
            "{# object: t = a/b #}{# anchor: t.field #}",
            "{# anchor: undeclared #}",
            "{# project: undeclared #}",
        ],
    )
    def test_refuses(self, template: str) -> None:
        with pytest.raises(OverlayError):
            overlay.world_placement(template)

    def test_a_placing_alias_is_mirrored_unread(self) -> None:
        """An anchor needs its object even if the template reads no field of it."""
        template = "{# object: t = a/b #}{# anchor: t #}" + SQUARE
        assert overlay.resolve_objects(template) == {"t": "a/b"}


class TestAnchor:
    def test_centred_on_the_object(self, published, target, view_from) -> None:
        """10 m right at 100 m with a 90° lens on 160 px: 8 px right of centre."""
        t = target(ned(100, 10, 0))
        w = widget(
            published,
            f"{{# object: t = {t.synq_absolute_path} #}}{{# anchor: t #}}" + SQUARE,
        )
        cam = view_from(w)

        assert "translate(78,50)" in markup_of(cam, "translate(")

    def test_view_is_in_the_template(self, published, target, view_from) -> None:
        t = target(ned(100, 0, 0))
        w = widget(
            published,
            f"{{# object: t = {t.synq_absolute_path} #}}{{# anchor: t #}}"
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
            "<text>{{ view.range | round | int }} m, r {{ view.r | round(1) }}</text></svg>",
        )
        cam = view_from(w)

        # ±1 m level and ±1 m plumb at 100 m, with fx = 80: the cylinder's
        # corner is √2 m out, 1.1 px.
        assert "100 m, r 1.1" in markup_of(cam, " m, r ")

    def test_behind_the_camera_is_not_drawn(self, published, target, view_from) -> None:
        behind, ahead = target(ned(-100, 0, 0)), target(ned(100, 0, 0))
        w = widget(
            published,
            f"{{# object: t = {behind.synq_absolute_path} #}}{{# anchor: t #}}" + SQUARE,
        )
        cam = view_from(w)
        wait_for(lambda: behind.synq_absolute_path in cam._overlay_objects)
        assert cam.render_overlays_for_client(160, 120) == ""

        # Rebound to one ahead, the same widget shows.
        cam.overlay_widgets[w.synq_absolute_path] = {"bindings": {"t": ahead.synq_absolute_path}}
        assert "translate(70,50)" in markup_of(cam, "translate(")

    def test_out_of_shot_is_not_drawn(self, published, target, view_from) -> None:
        t = target(ned(100, 500, 0))
        w = widget(
            published,
            f"{{# object: t = {t.synq_absolute_path} #}}{{# anchor: t #}}" + SQUARE,
        )
        cam = view_from(w)
        wait_for(lambda: t.synq_absolute_path in cam._overlay_objects)

        assert cam.render_overlays_for_client(160, 120) == ""

    def test_an_unplaced_camera_draws_nothing(self, published, target, camera) -> None:
        t = target(ned(100, 0, 0))
        w = widget(
            published,
            f"{{# object: t = {t.synq_absolute_path} #}}{{# anchor: t #}}" + SQUARE,
        )
        cam = camera(
            "testimage://",
            max_width=160,
            max_height=120,
            overlay_widgets={w.synq_absolute_path: {}},
            overlay_client_render=True,
            synq_auto_start=True,
        )
        cam.set_intrinsics("manual", hfov=90.0)
        cam.start(background=False)
        cam.read()
        wait_for(lambda: t.synq_absolute_path in cam._overlay_objects)

        assert cam.render_overlays_for_client(160, 120) == ""

    def test_follows_the_canvas(self, published, target, view_from) -> None:
        """On a canvas twice the frame's size, it is centred twice as far out."""
        t = target(ned(100, 10, 0))
        w = widget(
            published,
            f"{{# object: t = {t.synq_absolute_path} #}}{{# anchor: t #}}" + SQUARE,
        )
        cam = view_from(w)
        markup_of(cam, "translate(")

        assert "translate(166,110)" in cam.render_overlays_for_client(160, 120, 320, 240)

    def test_burned_into_the_frame(self, published, target, camera) -> None:
        """The server-rendered path places it the same way."""
        import cv2
        import numpy as np

        t = target(ned(100, 0, 0))
        w = widget(
            published,
            f"{{# object: t = {t.synq_absolute_path} #}}{{# anchor: t #}}" + SQUARE,
        )
        cam = camera(
            "testimage://",
            max_width=160,
            max_height=120,
            overlay_widgets={w.synq_absolute_path: {}},
            synq_auto_start=True,
        )
        cam.relative_to = RootFrame("ECEF")
        cam.mount = Pose(
            offset=Offset.from_geodetic(LAT, LON, HEIGHT),
            orientation=Orientation.ned_to_ecef(LAT, LON),
        )
        cam.set_intrinsics("manual", hfov=90.0)
        cam.start(background=False)

        def red_at_centre() -> bool:
            decoded = cv2.imdecode(np.frombuffer(cam.read(), np.uint8), cv2.IMREAD_COLOR)
            blue, green, red = (int(c) for c in decoded[60, 80])
            return red > 200 and blue < 60 and green < 60

        wait_for(red_at_centre)


class TestAnchorEach:
    def test_one_copy_per_link(self, published, target, view_from) -> None:
        left, right = target(ned(100, -10, 0)), target(ned(100, 10, 0))
        holder = published(Holder, targets=(Link.to(left), Link.to(right)))
        w = widget(
            published,
            f"{{# object: h = {holder.synq_absolute_path} #}}{{# anchor: each h.targets #}}"
            + SQUARE,
        )
        cam = view_from(w)

        markup = markup_of(cam, "translate(78,50)")
        assert "translate(62,50)" in markup

    def test_item_is_the_linked_object(self, published, target, view_from) -> None:
        one = target(ned(100, 0, 0))
        holder = published(Holder, targets=(Link.to(one),))
        w = widget(
            published,
            f"{{# object: h = {holder.synq_absolute_path} #}}{{# anchor: each h.targets #}}"
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
            "<text>{{ item.relative_to }}</text></svg>",
        )
        cam = view_from(w)

        assert "<text>ECEF</text>" in markup_of(cam, "<text>")

    def test_follows_the_links_changing(self, published, target, view_from) -> None:
        one, two = target(ned(100, -10, 0)), target(ned(100, 10, 0))
        holder = published(Holder, targets=(Link.to(one),))
        w = widget(
            published,
            f"{{# object: h = {holder.synq_absolute_path} #}}{{# anchor: each h.targets #}}"
            + SQUARE,
        )
        cam = view_from(w)
        markup_of(cam, "translate(62,50)")

        holder.targets = (Link.to(two),)

        def moved() -> bool:
            markup = cam.render_overlays_for_client(160, 120)
            return "translate(78,50)" in markup and "translate(62,50)" not in markup

        wait_for(moved)


class TestProject:
    def test_pixels_of_each_alias(self, published, target, view_from) -> None:
        a, behind = target(ned(100, 10, 10)), target(ned(-100, 0, 0))
        w = widget(
            published,
            f"{{# object: a = {a.synq_absolute_path} #}}"
            f"{{# object: b = {behind.synq_absolute_path} #}}"
            "{# project: a, b #}"
            '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" '
            'viewBox="0 0 {{ frame.width }} {{ frame.height }}">'
            "<text>{{ view.a.x | round | int }},{{ view.a.y | round | int }}"
            " {{ 'gone' if view.b is none else 'here' }}</text></svg>",
        )
        cam = view_from(w)

        assert "88,68 gone" in markup_of(cam, "gone")

    def test_is_placed_on_the_frame(self, published, target, view_from) -> None:
        a = target(ned(100, 0, 0))
        w = widget(
            published,
            f"{{# object: a = {a.synq_absolute_path} #}}{{# project: a #}}" + SQUARE,
        )
        cam = view_from(w)

        assert "translate(0,0)" in markup_of(cam, "translate(")
