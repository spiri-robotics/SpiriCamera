"""Synthetic test-pattern source backed by bundled SVGs and, for
:py:data:`YOLO_DEMO_IMAGE`, an optional real photo.

This module owns the test image library end to end: finding the SVGs
that ship with the package, locating the one raster pattern (borrowed
at runtime from an optional dependency rather than bundled -- see
:py:func:`raster_test_images`), rendering both, and serving them as
frames.  Nothing outside this module needs to know the patterns exist.
"""

from __future__ import annotations

import math
import re
import time
import xml.etree.ElementTree as ElementTree
from functools import cache
from importlib.resources import files
from pathlib import Path

import cv2
import numpy as np
import resvg_py
from loguru import logger

from SpiriCamera.sources.base import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceURL,
)

#: Served when ``testimage://`` is given with no pattern name.
DEFAULT_IMAGE = "pm5544"

#: Name of the optional, ultralytics-provided photo used for ML testing.
YOLO_DEMO_IMAGE = "yolo_demo"

#: Fraction of the source photo's width/height kept in each crop.  The
#: remaining margin is how far the pan in :py:func:`_pan_window` can move
#: before it would run off the edge of the source image.
_PAN_CROP_FRACTION = 0.8

#: Seconds for one full revolution of the slow circular pan.
_PAN_PERIOD_SECONDS = 24.0

#: How often to summarize render-rate debug logging for animated
#: patterns, which would otherwise log once per frame.
_RENDER_LOG_INTERVAL_SECONDS = 5.0


@cache
def test_images() -> dict[str, str]:
    """Load the bundled SVG test patterns.

    Returns
    -------
    dict[str, str]
        Pattern name (the file stem) mapped to its SVG source.
    """
    directory = Path(str(files("SpiriCamera") / "test_images"))
    if not directory.is_dir():
        logger.warning(f"No test image directory at {directory}")
        return {}

    images = {
        svg.stem: svg.read_text(encoding="utf-8") for svg in directory.glob("*.svg")
    }
    logger.debug(f"Loaded {len(images)} test image(s): {', '.join(sorted(images))}")
    return images


@cache
def raster_test_images() -> dict[str, Path]:
    """Locate optional raster test images, not bundled with this package.

    Unlike the SVG patterns in :py:func:`test_images`, ``yolo_demo`` is a
    real photograph, and real photographs come with a copyright that a
    git repository cannot casually redistribute. Rather than bundle one,
    this reaches into the ``ultralytics`` package -- an existing optional
    dependency (the ``examples`` extra) that already carries a couple of
    sample photos for its own demos -- and serves one of those in place.

    Returns
    -------
    dict[str, Path]
        Pattern name mapped to the on-disk photo, or empty if
        ``ultralytics`` is not installed.
    """
    try:
        import ultralytics
    except ImportError:
        return {}

    path = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
    if not path.is_file():
        return {}
    return {YOLO_DEMO_IMAGE: path}


@cache
def _load_raster(path: Path) -> np.ndarray:
    """Decode a raster test image once and cache the array."""
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise SourceError(f"Could not decode test image at {path}")
    return image


def _pan_window(
    width: int, height: int, crop_width: int, crop_height: int
) -> tuple[int, int]:
    """Slide a crop window slowly around the centre of an image, in a circle.

    Parameters
    ----------
    width, height : int
        The full source image's dimensions.
    crop_width, crop_height : int
        The crop window's dimensions; must not exceed ``width``/``height``.

    Returns
    -------
    tuple[int, int]
        The crop window's top-left corner for the current time.
    """
    radius_x = (width - crop_width) / 2
    radius_y = (height - crop_height) / 2
    angle = (time.monotonic() % _PAN_PERIOD_SECONDS) / _PAN_PERIOD_SECONDS * 2 * math.pi
    center_x = width / 2 + radius_x * math.cos(angle)
    center_y = height / 2 + radius_y * math.sin(angle)
    x0 = round(center_x - crop_width / 2)
    y0 = round(center_y - crop_height / 2)
    return (
        max(0, min(width - crop_width, x0)),
        max(0, min(height - crop_height, y0)),
    )


def svg_aspect_ratio(svg: str) -> float | None:
    """Read an SVG's intrinsic width-to-height ratio.

    Parameters
    ----------
    svg : str
        The SVG source.

    Returns
    -------
    float | None
        Width divided by height, or ``None`` if the SVG declares
        neither a ``viewBox`` nor usable ``width`` and ``height``.
    """
    try:
        root = ElementTree.fromstring(svg.encode("utf-8"))
    except ElementTree.ParseError:
        return None

    view_box = root.get("viewBox")
    if view_box:
        parts = re.split(r"[,\s]+", view_box.strip())
        if len(parts) == 4:
            try:
                _, _, width, height = (float(part) for part in parts)
            except ValueError:
                width = height = 0.0
            if width > 0 and height > 0:
                return width / height

    width = _svg_length(root.get("width"))
    height = _svg_length(root.get("height"))
    if width and height:
        return width / height
    return None


def fit_inside(ratio: float | None, max_width: int, max_height: int) -> tuple[int, int]:
    """Find the largest size with the given ratio that fits in a box.

    ``max_width`` and ``max_height`` are a bounding box, not a target
    shape, so a 4:3 pattern asked for at 1920x1080 renders 1440x1080
    rather than being stretched to fill the 16:9 frame.

    Parameters
    ----------
    ratio : float | None
        Desired width-to-height ratio.  ``None`` fills the box.
    max_width : int
        Largest acceptable width.
    max_height : int
        Largest acceptable height.

    Returns
    -------
    tuple[int, int]
        The fitted width and height, each at least 1.
    """
    if not ratio or ratio <= 0:
        return max(1, max_width), max(1, max_height)

    width = min(max_width, round(max_height * ratio))
    height = round(width / ratio)
    return max(1, int(width)), max(1, int(height))


def _svg_length(value: str | None) -> float | None:
    """Parse an SVG length, ignoring any unit suffix."""
    if not value:
        return None
    match = re.match(r"\s*([0-9.]+)", value)
    if not match:
        return None
    try:
        length = float(match.group(1))
    except ValueError:
        return None
    return length or None


class TestImageSource(SourceBase):
    """Renders a bundled test pattern at the requested resolution.

    Most patterns are SVGs, rasterised on demand and cached per
    resolution, so changing the camera's requested size re-renders
    exactly once. :py:data:`YOLO_DEMO_IMAGE` is the exception: it is a
    real photo (see :py:func:`raster_test_images`), served with a slow
    circular pan so it also exercises a moving-frame overlay, and is
    therefore re-rendered on every read regardless of resolution.
    """

    schemes = ("testimage",)

    def __init__(self, url: SourceURL) -> None:
        """Create a test image handler.

        Parameters
        ----------
        url : SourceURL
            A parsed ``testimage://`` URL.
        """
        super().__init__(url)
        self._image_name = url.target or DEFAULT_IMAGE
        self._animated = self._image_name in raster_test_images()
        self._frame: np.ndarray | None = None
        self._frame_resolution: tuple[int, int] | None = None
        self._ratio: float | None = None
        self._ratio_parsed = False
        self._open = False
        self._render_log_count = 0
        self._render_log_since: float | None = None

    @classmethod
    def from_url(cls, url: SourceURL) -> TestImageSource:
        """Resolve the pattern name, failing early if it is unknown.

        Parameters
        ----------
        url : SourceURL
            A parsed ``testimage://`` URL.

        Returns
        -------
        TestImageSource
            A handler for the named pattern.

        Raises
        ------
        SourceError
            If the named pattern is not available.
        """
        name = url.target or DEFAULT_IMAGE
        available = {*test_images(), *raster_test_images()}
        if name not in available:
            hint = ""
            if name == YOLO_DEMO_IMAGE:
                hint = " Install the 'examples' extra (uv sync --extra examples) to provide it."
            raise SourceError(
                f"Unknown test image {name!r}.{hint} "
                f"Available: {', '.join(sorted(available)) or 'none'}"
            )
        return cls(url)

    @property
    def target(self) -> str:
        """The pattern name, with the default filled in."""
        return self._image_name

    @property
    def is_open(self) -> bool:
        """Whether the source is currently serving frames."""
        return self._open

    def open(self, settings: CaptureSettings) -> SourceCapabilities:
        """Start serving the pattern.

        Parameters
        ----------
        settings : CaptureSettings
            Requested capture settings; a rendered pattern honours any
            resolution, so these only seed the first render.

        Returns
        -------
        SourceCapabilities
            Capabilities with no resolution or framerate limits.
        """
        self._invalidate()
        self._open = True
        logger.info(
            f"{self!r} serving {self._image_name} at "
            f"{settings.max_width}x{settings.max_height}"
        )
        return SourceCapabilities(
            vendor="SpiriCamera", model=f"testimage:{self._image_name}"
        )

    def close(self) -> None:
        """Stop serving and drop the cached render."""
        self._open = False
        self._invalidate()
        logger.debug(f"{self!r} closed")

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        """Return the pattern rendered to fit the requested bounds.

        The pattern keeps its own aspect ratio and is fitted inside
        ``max_width`` x ``max_height`` rather than stretched to fill it.

        The same array object is returned until the resolution changes,
        which lets the camera skip re-encoding an unchanged frame.

        Parameters
        ----------
        settings : CaptureSettings
            Bounds the render resolution.

        Returns
        -------
        np.ndarray | None
            The rendered BGR frame.

        Raises
        ------
        SourceError
            If the pattern cannot be rasterised.
        """
        resolution = fit_inside(
            self._aspect_ratio(),
            max(1, settings.max_width),
            max(1, settings.max_height),
        )
        if (
            not self._animated
            and self._frame is not None
            and self._frame_resolution == resolution
        ):
            return self._frame

        self._frame = self._render(resolution)
        self._frame_resolution = resolution
        self._log_render_rate(resolution)
        return self._frame

    def _log_render_rate(self, resolution: tuple[int, int]) -> None:
        """Summarize renders rather than logging every one.

        An animated pattern re-renders every frame, so a per-render
        debug line would flood the log at the capture framerate. Count
        renders instead and flush one summary line every
        :py:data:`_RENDER_LOG_INTERVAL_SECONDS`.
        """
        now = time.monotonic()
        if self._render_log_since is None:
            self._render_log_since = now
        self._render_log_count += 1
        elapsed = now - self._render_log_since
        if elapsed < _RENDER_LOG_INTERVAL_SECONDS:
            return
        rate = self._render_log_count / elapsed
        logger.debug(
            f"{self!r} rendered {self._render_log_count} frame(s) at "
            f"{resolution[0]}x{resolution[1]} in {elapsed:.1f}s ({rate:.1f} fps)"
        )
        self._render_log_count = 0
        self._render_log_since = now

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _aspect_ratio(self) -> float | None:
        """The pattern's intrinsic ratio, parsed once and remembered."""
        if not self._ratio_parsed:
            if self._animated:
                path = raster_test_images()[self._image_name]
                height, width = _load_raster(path).shape[:2]
                self._ratio = width / height
            else:
                self._ratio = svg_aspect_ratio(test_images().get(self._image_name, ""))
            self._ratio_parsed = True
            logger.debug(f"{self!r} intrinsic aspect ratio: {self._ratio}")
        return self._ratio

    def _render(self, resolution: tuple[int, int]) -> np.ndarray:
        """Rasterise the pattern to a BGR array at the given resolution."""
        if self._animated:
            return self._render_raster(resolution)
        return self._render_svg(resolution)

    def _render_svg(self, resolution: tuple[int, int]) -> np.ndarray:
        """Rasterise the SVG pattern to a BGR array."""
        svg = test_images().get(self._image_name)
        if svg is None:
            raise SourceError(f"Test image {self._image_name!r} is no longer available")

        width, height = resolution
        try:
            png_bytes = resvg_py.svg_to_bytes(
                svg_string=svg, width=width, height=height
            )
        except Exception as exc:
            raise SourceError(
                f"Failed to render test image {self._image_name!r}: {exc}"
            ) from exc

        frame = cv2.imdecode(
            np.frombuffer(bytes(png_bytes), dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if frame is None:
            raise SourceError(
                f"Rendered test image {self._image_name!r} could not be decoded"
            )
        return frame

    def _render_raster(self, resolution: tuple[int, int]) -> np.ndarray:
        """Crop a slowly-panning window out of a raster photo and resize it.

        The window's size is fixed (:py:data:`_PAN_CROP_FRACTION` of the
        source photo); only its position moves, tracing a slow circle
        around the photo's centre via :py:func:`_pan_window`. Detected
        objects therefore stay in frame throughout the pan rather than
        drifting off the edge.
        """
        path = raster_test_images().get(self._image_name)
        if path is None:
            raise SourceError(f"Test image {self._image_name!r} is no longer available")

        source = _load_raster(path)
        height, width = source.shape[:2]
        crop_width = max(1, round(width * _PAN_CROP_FRACTION))
        crop_height = max(1, round(height * _PAN_CROP_FRACTION))
        x0, y0 = _pan_window(width, height, crop_width, crop_height)
        crop = source[y0 : y0 + crop_height, x0 : x0 + crop_width]
        return cv2.resize(crop, resolution, interpolation=cv2.INTER_AREA)

    def _invalidate(self) -> None:
        """Drop the cached render so the next read re-rasterises."""
        self._frame = None
        self._frame_resolution = None
