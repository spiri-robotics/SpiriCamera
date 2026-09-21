"""Tests for SVG HUD overlays: templating, rasterizing, compositing."""

from __future__ import annotations

import numpy as np
import pytest

from SpiriCamera import overlay


class TestDeclaredObjects:
    """Parsing `{# object: alias = default/topic #}` declarations."""

    def test_finds_a_declaration(self) -> None:
        template = "{# object: battery = azrael/battery_monitor #}\n<svg></svg>"

        assert overlay.declared_objects(template) == {
            "battery": "azrael/battery_monitor"
        }

    def test_finds_several_declarations(self) -> None:
        template = (
            "{# object: battery = azrael/battery_monitor #}\n"
            "{# object: cam = azrael/spiricamera_testimage #}\n"
            "<svg></svg>"
        )

        assert overlay.declared_objects(template) == {
            "battery": "azrael/battery_monitor",
            "cam": "azrael/spiricamera_testimage",
        }

    def test_hyphens_in_the_topic_are_fine(self) -> None:
        """The alias must be a plain identifier; the default topic on
        the right of `=` is free-form -- that's the whole point."""
        template = "{# object: cam = spiri-cam-01/cam #}"

        assert overlay.declared_objects(template) == {"cam": "spiri-cam-01/cam"}

    def test_no_declarations_is_empty(self) -> None:
        assert overlay.declared_objects("<svg></svg>") == {}


class TestResolveObjects:
    """Cross-checking what a template reads against what it declares."""

    def test_resolves_a_declared_and_used_alias(self) -> None:
        template = (
            "{# object: battery = azrael/battery_monitor #}\n"
            "{{ objects.battery.voltage }}"
        )

        assert overlay.resolve_objects(template) == {
            "battery": "azrael/battery_monitor"
        }

    def test_ignores_a_declared_but_unused_alias(self) -> None:
        """Declaring an object is not the same as mirroring it -- only
        what the render body actually reads is returned."""
        template = "{# object: battery = azrael/battery_monitor #}\n<svg></svg>"

        assert overlay.resolve_objects(template) == {}

    def test_raises_on_an_undeclared_alias(self) -> None:
        """The enforcement point: reading an alias with no declaration
        is a hard error, not a silent no-op."""
        with pytest.raises(overlay.OverlayError):
            overlay.resolve_objects("{{ objects.battery.voltage }}")

    def test_ignores_exif_and_other_names(self) -> None:
        template = "{{ exif.make }} {{ standalone }}"

        assert overlay.resolve_objects(template) == {}

    def test_only_the_first_segment_after_objects_is_the_alias(self) -> None:
        """`objects.battery.voltage` reads field `voltage` off the
        `battery` alias -- unlike the old flat `topics.*` scalars, this
        nesting is the expected shape now that an alias names a whole
        object."""
        template = (
            "{# object: battery = azrael/battery_monitor #}\n"
            "{{ objects.battery.voltage }} {{ objects.battery.current }}"
        )

        assert overlay.resolve_objects(template) == {
            "battery": "azrael/battery_monitor"
        }


class FakeCamera:
    """Just enough of a ``Camera`` for :py:func:`overlay.camera_metrics_widget`.

    Only ``synq_topic``/``synq_absolute_path`` are read at build time --
    real field values are supplied separately, straight to
    ``render_svg``, so this needs no zenoh session of its own.
    """

    def __init__(self, synq_topic: str, synq_absolute_path: str) -> None:
        self.synq_topic = synq_topic
        self.synq_absolute_path = synq_absolute_path


class TestCameraMetricsWidget:
    """The ready-made widget showing a camera's own live stats."""

    def test_topic_is_derived_from_the_camera(self) -> None:
        camera = FakeCamera(
            "spiricamera_testimage", "spiri-cam-01/spiricamera_testimage"
        )

        widget = overlay.camera_metrics_widget(camera)
        try:
            assert widget.synq_topic == "spiricamera_testimage_metrics"
        finally:
            widget.close()

    def test_declares_the_camera_itself_as_the_cam_alias_s_default(self) -> None:
        """The hostname prefix routinely has a hyphen, so the declared
        default topic (not the alias) has to carry it -- resolve_objects
        confirms both halves (declared and used) line up."""
        camera = FakeCamera("cam", "spiri-cam-01/cam")

        widget = overlay.camera_metrics_widget(camera)
        try:
            assert overlay.resolve_objects(widget.svg_template) == {
                "cam": "spiri-cam-01/cam",
            }
        finally:
            widget.close()

    def test_renders_the_camera_s_live_values(self) -> None:
        camera = FakeCamera("cam", "spiri-cam-01/cam")

        widget = overlay.camera_metrics_widget(camera)
        try:
            svg = overlay.render_svg(
                widget.svg_template,
                objects={"cam": {"quality": 80}},
                exif_tags={"timestamp": "1234567890.5"},
                frame_info={
                    "width": 1900,
                    "height": 1070,
                    "framerate": 29.5,
                },
            )
        finally:
            widget.close()

        assert "1900x1070 29.5fps q=80" in svg
        assert "1234567890.5" in svg

    def test_shows_untagged_when_exif_is_disabled(self) -> None:
        """``Camera.exif_enabled = False`` leaves ``exif_tags`` empty --
        the widget should degrade gracefully, not fail to render."""
        camera = FakeCamera("cam", "spiri-cam-01/cam")

        widget = overlay.camera_metrics_widget(camera)
        try:
            svg = overlay.render_svg(
                widget.svg_template,
                objects={"cam": {"quality": 80}},
                exif_tags={},
                frame_info={"width": 1900, "height": 1070, "framerate": 29.5},
            )
        finally:
            widget.close()

        assert "untagged" in svg


class TestTigerWidget:
    """The ready-made percentage-sized example widget."""

    def test_topic_is_derived_from_the_camera(self) -> None:
        camera = FakeCamera(
            "spiricamera_testimage", "spiri-cam-01/spiricamera_testimage"
        )

        widget = overlay.tiger_widget(camera)
        try:
            assert widget.synq_topic == "spiricamera_testimage_tiger"
        finally:
            widget.close()

    def test_bakes_size_percent_into_the_root_svg(self) -> None:
        camera = FakeCamera("cam", "spiri-cam-01/cam")

        widget = overlay.tiger_widget(camera, size_percent=35)
        try:
            assert overlay._percent_size(widget.svg_template) == (35.0, 35.0)
        finally:
            widget.close()

    def test_default_size_is_20_percent(self) -> None:
        camera = FakeCamera("cam", "spiri-cam-01/cam")

        widget = overlay.tiger_widget(camera)
        try:
            assert overlay._percent_size(widget.svg_template) == (20.0, 20.0)
        finally:
            widget.close()

    def test_renders_to_a_raster_scaled_against_the_frame(self) -> None:
        """End to end: the bundled artwork, once templated and rendered
        through the same path `_render_overlays` uses, actually scales
        against the frame rather than its own 900x900 viewBox."""
        camera = FakeCamera("cam", "spiri-cam-01/cam")

        widget = overlay.tiger_widget(camera, size_percent=20)
        try:
            svg = overlay.render_svg(
                widget.svg_template,
                objects={},
                exif_tags={},
                frame_info={},
            )
            raster = overlay.rasterize(svg, frame_width=1000, frame_height=1000)
        finally:
            widget.close()

        assert raster.shape == (200, 200, 4)
        assert np.count_nonzero(raster[:, :, 3]) > 0


class TestRenderSvg:
    """Templating a widget's SVG against objects and EXIF."""

    def test_substitutes_objects_and_exif(self) -> None:
        rendered = overlay.render_svg(
            "V={{ objects.battery.voltage }} M={{ exif.make }}",
            objects={"battery": {"voltage": 12.3}},
            exif_tags={"make": "Spiri"},
            frame_info={},
        )

        assert rendered == "V=12.3 M=Spiri"

    def test_substitutes_frame_info(self) -> None:
        rendered = overlay.render_svg(
            "{{ frame.width }}x{{ frame.height }} @ {{ frame.framerate }}",
            objects={},
            exif_tags={},
            frame_info={"width": 640, "height": 480, "framerate": 24.9},
        )

        assert rendered == "640x480 @ 24.9"

    def test_missing_object_raises(self) -> None:
        """An alias with no value yet is a per-frame failure, not
        silently rendered as empty."""
        with pytest.raises(overlay.OverlayError):
            overlay.render_svg(
                "{{ objects.battery.voltage }}", objects={}, exif_tags={}, frame_info={}
            )

    def test_malformed_template_raises(self) -> None:
        with pytest.raises(overlay.OverlayError):
            overlay.render_svg(
                "{{ unterminated", objects={}, exif_tags={}, frame_info={}
            )


class TestRasterize:
    """Rendering an SVG to a raster at its own natural size."""

    def test_matches_declared_size(self) -> None:
        """No forced scaling: the raster is exactly the SVG's own size."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
            '<rect width="40" height="20" fill="red"/></svg>'
        )

        raster = overlay.rasterize(svg)

        assert raster.shape == (20, 40, 4)
        assert raster.dtype == np.uint8

    def test_pixel_colors_are_rgba(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            '<rect width="10" height="10" fill="blue"/></svg>'
        )

        raster = overlay.rasterize(svg)

        assert tuple(raster[5, 5]) == (0, 0, 255, 255)

    def test_transparent_background(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            '<rect x="2" y="2" width="2" height="2" fill="red"/></svg>'
        )

        raster = overlay.rasterize(svg)

        assert tuple(raster[0, 0]) == (0, 0, 0, 0)

    def test_unparsable_svg_raises(self) -> None:
        with pytest.raises(overlay.OverlayError):
            overlay.rasterize("not an svg at all")

    def test_text_using_the_bundled_font_actually_draws_pixels(self) -> None:
        """Regression test: thorvg does not fall back to any OS font, so
        `<text>` with no matching loaded font silently draws nothing --
        this is what that looked like before overlay.DEFAULT_FONT was
        loaded from a real file path at import time (font_load_data()
        from in-memory bytes returns NOT_SUPPORTED in this thorvg build;
        font_load() from a path does not)."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="30">'
            f'<text x="4" y="22" font-family="{overlay.DEFAULT_FONT}" '
            'font-size="20" fill="white">1920x1080</text></svg>'
        )

        raster = overlay.rasterize(svg)

        assert np.count_nonzero(raster[:, :, 3]) > 0

    def test_an_unrecognized_font_family_falls_back_to_the_loaded_font(self) -> None:
        """thorvg's font cache is process-global, not per-widget: once
        DEFAULT_FONT is loaded at import time, any font-family that
        names nothing else loaded still draws, via that same fallback --
        it does not silently blank out. The real failure mode this
        module guards against is nothing being loaded *at all* (see the
        module-level font_load() call and its Result check), which
        cannot be demonstrated in-process once that has already
        succeeded once for this test run."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="30">'
            '<text x="4" y="22" font-family="NotARealLoadedFont" '
            'font-size="20" fill="white">1920x1080</text></svg>'
        )

        raster = overlay.rasterize(svg)

        assert np.count_nonzero(raster[:, :, 3]) > 0

    def test_percent_size_is_scaled_against_the_frame(self) -> None:
        """A square 100x100 viewBox, told to fill 20% of a 1000x1000
        frame, ends up exactly 200x200 -- 20% of each frame dimension."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20%" height="20%" '
            'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
        )

        raster = overlay.rasterize(svg, frame_width=1000, frame_height=1000)

        assert raster.shape == (200, 200, 4)

    def test_percent_size_preserves_aspect_ratio(self) -> None:
        """A square viewBox asked to fill 20% of a non-square frame is
        capped by the *smaller* resulting dimension, not stretched to
        fill both -- the frame is 1000 wide but only 500 tall, so 20%
        of height (100px) is the binding constraint, not 20% of width
        (200px)."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20%" height="20%" '
            'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
        )

        raster = overlay.rasterize(svg, frame_width=1000, frame_height=500)

        assert raster.shape == (100, 100, 4)

    def test_percent_size_without_frame_dimensions_raises(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20%" height="20%" '
            'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
        )

        with pytest.raises(overlay.OverlayError):
            overlay.rasterize(svg)

    def test_mixed_percent_and_absolute_size_is_not_percent_sizing(self) -> None:
        """All-or-nothing: a percentage ``width`` with an absolute
        ``height`` is not scaled against the frame -- there is no
        meaning for that combination, so it is rendered as an ordinary,
        unscaled widget instead (and thorvg resolves the lone
        percentage itself, against no viewBox, to a near-zero size)."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20%" height="20" '
            'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
        )

        raster = overlay.rasterize(svg, frame_width=1000, frame_height=1000)

        assert raster.shape == (20, 20, 4)


class TestMeasureSize:
    """The pixel size `rasterize()` would use, without rasterizing."""

    def test_matches_declared_size(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
            '<rect width="40" height="20" fill="red"/></svg>'
        )

        assert overlay.measure_size(svg) == (40, 20)

    def test_matches_rasterize_for_percent_size(self) -> None:
        """Same scaled result `rasterize()` would draw at, for a
        percentage-sized widget -- same call, no frame size wasted on a
        software rendering pass."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20%" height="20%" '
            'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
        )

        size = overlay.measure_size(svg, frame_width=1000, frame_height=500)

        assert size == (100, 100)

    def test_percent_size_without_frame_dimensions_raises(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20%" height="20%" '
            'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
        )

        with pytest.raises(overlay.OverlayError):
            overlay.measure_size(svg)

    def test_unparsable_svg_raises(self) -> None:
        with pytest.raises(overlay.OverlayError):
            overlay.measure_size("not an svg at all")


class TestNormalizeSvgSize:
    """Overwriting a root `<svg>`'s declared size for client rendering."""

    def test_absolute_size_is_replaced(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
            '<rect width="40" height="20" fill="red"/></svg>'
        )

        result = overlay._normalize_svg_size(svg, 80, 40)

        assert result.startswith(
            '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
        )
        assert '<rect width="40" height="20" fill="red"/>' in result

    def test_percent_size_is_replaced(self) -> None:
        """The exact case client rendering needs: a percent-sized
        widget's box pinned to the pre-computed, aspect-fit pixel size
        `measure_size()` returned, not left to the browser to letterbox
        inside the full percentage box."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20%" height="20%" '
            'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
        )

        result = overlay._normalize_svg_size(svg, 100, 100)

        assert 'width="20%"' not in result
        assert 'width="100"' in result
        assert 'height="100"' in result
        assert 'viewBox="0 0 100 100"' in result

    def test_only_the_root_tag_is_touched(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            '<svg width="5" height="5"><rect width="5" height="5"/></svg></svg>'
        )

        result = overlay._normalize_svg_size(svg, 20, 20)

        assert result.startswith(
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        )
        assert '<svg width="5" height="5">' in result


class TestAnchorPosition:
    """Placing a widget's top-left corner given an anchor."""

    @pytest.mark.parametrize(
        ("anchor", "expected"),
        [
            ("top_left", (0, 0)),
            ("top_middle", (40, 0)),
            ("top_right", (80, 0)),
            ("center_left", (0, 20)),
            ("center_middle", (40, 20)),
            ("center_right", (80, 20)),
            ("bottom_left", (0, 40)),
            ("bottom_middle", (40, 40)),
            ("bottom_right", (80, 40)),
        ],
    )
    def test_named_anchors(self, anchor: str, expected: tuple[int, int]) -> None:
        """A 100x50 frame with a 20x10 widget, flush against each edge or
        centred, depending on the anchor."""
        assert overlay.anchor_position(anchor, 0.0, 0.0, 100, 50, 20, 10) == expected

    def test_custom_uses_percent_as_top_left(self) -> None:
        """Unlike the named anchors, ``custom`` ignores widget size: the
        percent names the top-left corner directly."""
        assert overlay.anchor_position("custom", 25.0, 50.0, 200, 100, 20, 10) == (
            50,
            50,
        )

    def test_unknown_anchor_falls_back_to_top_left(self) -> None:
        assert overlay.anchor_position("nonsense", 0.0, 0.0, 100, 50, 20, 10) == (0, 0)


class TestComposite:
    """Alpha-blending a widget raster onto a frame."""

    def test_opaque_pixel_replaces_frame(self) -> None:
        frame = np.zeros((10, 10, 3), np.uint8)
        rgba = np.zeros((2, 2, 4), np.uint8)
        rgba[..., :] = (10, 20, 30, 255)  # opaque RGBA

        result = overlay.composite(frame, rgba, 0, 0)

        assert tuple(result[0, 0]) == (30, 20, 10)  # BGR

    def test_transparent_pixel_leaves_frame(self) -> None:
        frame = np.full((10, 10, 3), 99, np.uint8)
        rgba = np.zeros((2, 2, 4), np.uint8)  # fully transparent

        result = overlay.composite(frame, rgba, 0, 0)

        assert tuple(result[0, 0]) == (99, 99, 99)

    def test_half_transparent_pixel_blends(self) -> None:
        frame = np.zeros((10, 10, 3), np.uint8)
        rgba = np.zeros((1, 1, 4), np.uint8)
        rgba[0, 0] = (200, 0, 0, 128)  # ~50% red

        result = overlay.composite(frame, rgba, 0, 0)

        assert result[0, 0, 2] == pytest.approx(100, abs=2)  # B channel of BGR

    def test_out_of_bounds_position_is_clipped(self) -> None:
        """A widget placed entirely off-frame changes nothing, and does
        not raise."""
        frame = np.full((10, 10, 3), 7, np.uint8)
        rgba = np.full((4, 4, 4), 255, np.uint8)

        result = overlay.composite(frame, rgba, 100, 100)

        assert np.array_equal(result, frame)

    def test_partially_out_of_bounds_clips_to_frame(self) -> None:
        frame = np.zeros((10, 10, 3), np.uint8)
        rgba = np.full((4, 4, 4), 255, np.uint8)

        result = overlay.composite(frame, rgba, 8, 8)

        assert tuple(result[9, 9]) == (255, 255, 255)
        assert tuple(result[0, 0]) == (0, 0, 0)

    def test_never_mutates_the_input_frame(self) -> None:
        frame = np.zeros((5, 5, 3), np.uint8)
        rgba = np.full((2, 2, 4), 255, np.uint8)

        overlay.composite(frame, rgba, 0, 0)

        assert np.array_equal(frame, np.zeros((5, 5, 3), np.uint8))
