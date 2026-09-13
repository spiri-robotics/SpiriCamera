"""Tests for the synthetic test-pattern source."""

from __future__ import annotations

import pytest

from SpiriCamera.sources.base import CaptureSettings, SourceError
from SpiriCamera.sources.testimage import (
    DEFAULT_IMAGE,
    YOLO_DEMO_IMAGE,
    fit_inside,
    raster_test_images,
    svg_aspect_ratio,
    test_images as load_test_images,
)
from SpiriCamera.sources import resolve_source

#: Whether ultralytics (the "examples" extra) is installed, and so
#: whether the yolo_demo raster pattern is available to test against.
HAS_YOLO_DEMO = YOLO_DEMO_IMAGE in raster_test_images()

requires_yolo_demo = pytest.mark.skipif(
    not HAS_YOLO_DEMO, reason="yolo_demo needs the 'examples' extra (ultralytics)"
)

#: Small enough to keep rasterising cheap in tests.
SMALL = CaptureSettings(max_width=160, max_height=120)


class TestImageLibrary:
    """Loading the bundled patterns."""

    def test_bundled_pattern_is_available(self) -> None:
        """The package ships at least the default pattern."""
        assert DEFAULT_IMAGE in load_test_images()

    def test_patterns_are_svg_source(self) -> None:
        """Patterns are loaded as SVG text, not paths."""
        assert "<svg" in load_test_images()[DEFAULT_IMAGE]


class TestAspectRatio:
    """Reading an SVG's intrinsic shape."""

    def test_reads_bundled_pattern(self) -> None:
        """pm5544 is a 4:3 pattern."""
        assert svg_aspect_ratio(load_test_images()[DEFAULT_IMAGE]) == pytest.approx(4 / 3, abs=1e-3)

    def test_prefers_viewbox(self) -> None:
        """A viewBox wins over width and height attributes."""
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 16 9"/>'
        assert svg_aspect_ratio(svg) == pytest.approx(16 / 9)

    def test_falls_back_to_width_and_height(self) -> None:
        """Without a viewBox, the declared size is used."""
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="200px" height="100px"/>'
        assert svg_aspect_ratio(svg) == pytest.approx(2.0)

    @pytest.mark.parametrize(
        "svg",
        [
            '<svg xmlns="http://www.w3.org/2000/svg"/>',
            '<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0"/>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="bad"/>',
            "not xml at all",
        ],
    )
    def test_returns_none_when_undeclared(self, svg: str) -> None:
        """An SVG with no usable size reports no ratio rather than failing."""
        assert svg_aspect_ratio(svg) is None


class TestFitInside:
    """Fitting a ratio into a bounding box."""

    @pytest.mark.parametrize(
        ("ratio", "box", "expected"),
        [
            (4 / 3, (1920, 1080), (1440, 1080)),
            (4 / 3, (640, 480), (640, 480)),
            (4 / 3, (1280, 720), (960, 720)),
            (4 / 3, (300, 300), (300, 225)),
            (16 / 9, (640, 480), (640, 360)),
        ],
    )
    def test_fits_within_bounds(
        self, ratio: float, box: tuple[int, int], expected: tuple[int, int]
    ) -> None:
        """max_width and max_height bound the result, never stretch it."""
        fitted = fit_inside(ratio, *box)
        assert fitted == expected
        assert fitted[0] <= box[0]
        assert fitted[1] <= box[1]

    def test_no_ratio_fills_the_box(self) -> None:
        """Without an intrinsic ratio there is nothing to preserve."""
        assert fit_inside(None, 800, 600) == (800, 600)

    def test_never_returns_zero(self) -> None:
        """A degenerate box still yields a renderable size."""
        assert fit_inside(4 / 3, 1, 1) == (1, 1)


class TestTestImageSource:
    """The handler itself."""

    def test_defaults_to_the_default_pattern(self) -> None:
        """testimage:// with no name serves the default."""
        source = resolve_source("testimage://")
        assert source.target == DEFAULT_IMAGE

    def test_rejects_unknown_pattern_at_resolution(self) -> None:
        """A typo fails before anything is opened."""
        with pytest.raises(SourceError, match="Unknown test image"):
            resolve_source("testimage://nope")

    def test_reports_no_resolution_limits(self) -> None:
        """A rendered pattern has no maximum size."""
        source = resolve_source("testimage://")
        capabilities = source.open(SMALL)
        assert capabilities.max_supported_width is None
        assert capabilities.max_supported_height is None
        assert capabilities.max_supported_framerate is None

    def test_open_close_tracks_state(self) -> None:
        """is_open follows open() and close()."""
        source = resolve_source("testimage://")
        assert not source.is_open
        source.open(SMALL)
        assert source.is_open
        source.close()
        assert not source.is_open

    def test_renders_preserving_aspect_ratio(self) -> None:
        """A 4:3 pattern fits inside a 16:9 request without stretching."""
        source = resolve_source("testimage://")
        source.open(SMALL)

        frame = source.read(CaptureSettings(max_width=1920, max_height=1080))

        assert frame is not None
        height, width = frame.shape[:2]
        assert (width, height) == (1440, 1080)

    def test_caches_between_reads(self) -> None:
        """An unchanged resolution returns the very same array."""
        source = resolve_source("testimage://")
        source.open(SMALL)
        assert source.read(SMALL) is source.read(SMALL)

    def test_rerenders_when_resolution_changes(self) -> None:
        """A new bounding box produces a new frame."""
        source = resolve_source("testimage://")
        source.open(SMALL)
        first = source.read(SMALL)

        second = source.read(CaptureSettings(max_width=320, max_height=240))

        assert second is not first
        assert second is not None
        assert second.shape[:2] == (240, 320)

    def test_close_drops_the_cache(self) -> None:
        """Reopening does not serve a stale render."""
        source = resolve_source("testimage://")
        source.open(SMALL)
        first = source.read(SMALL)
        source.close()
        source.open(SMALL)

        assert source.read(SMALL) is not first

    def test_read_returns_bgr_frame(self) -> None:
        """Frames are three-channel arrays, not encoded bytes."""
        source = resolve_source("testimage://")
        source.open(SMALL)

        frame = source.read(SMALL)

        assert frame is not None
        assert frame.ndim == 3
        assert frame.shape[2] == 3


class TestYoloDemoImage:
    """The optional raster pattern used to exercise ML pipelines."""

    def test_missing_without_examples_extra(self) -> None:
        """Without ultralytics installed, the pattern is simply absent."""
        if HAS_YOLO_DEMO:
            pytest.skip("ultralytics is installed in this environment")
        with pytest.raises(SourceError, match="Unknown test image"):
            resolve_source(f"testimage://{YOLO_DEMO_IMAGE}")

    @requires_yolo_demo
    def test_reports_no_resolution_limits(self) -> None:
        """Like the SVG patterns, a rendered photo has no maximum size."""
        source = resolve_source(f"testimage://{YOLO_DEMO_IMAGE}")
        capabilities = source.open(SMALL)
        assert capabilities.max_supported_width is None
        assert capabilities.max_supported_height is None

    @requires_yolo_demo
    def test_read_returns_bgr_frame(self) -> None:
        """A cropped, resized photo is still a three-channel BGR array."""
        source = resolve_source(f"testimage://{YOLO_DEMO_IMAGE}")
        source.open(SMALL)

        frame = source.read(SMALL)

        assert frame is not None
        assert frame.ndim == 3
        assert frame.shape[2] == 3

    @requires_yolo_demo
    def test_moves_between_reads(self) -> None:
        """Unlike a static pattern, consecutive reads are not identical.

        The pan is slow (one revolution every 24s), so back-to-back
        reads land at slightly different points on the circle rather
        than an unchanging frame -- this is what lets the pattern
        exercise a moving-frame overlay.
        """
        source = resolve_source(f"testimage://{YOLO_DEMO_IMAGE}")
        source.open(SMALL)

        first = source.read(SMALL)
        second = source.read(SMALL)

        assert first is not second
