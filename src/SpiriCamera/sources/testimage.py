"""Synthetic test-pattern source backed by bundled SVGs.

This module owns the test image library end to end: finding the SVGs
that ship with the package, rasterising them, and serving them as
frames.  Nothing outside this module needs to know the patterns exist.
"""

from __future__ import annotations

import re
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

    images = {svg.stem: svg.read_text(encoding="utf-8") for svg in directory.glob("*.svg")}
    logger.debug(f"Loaded {len(images)} test image(s): {', '.join(sorted(images))}")
    return images


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
    """Renders a bundled SVG test pattern at the requested resolution.

    Useful for exercising the pipeline without hardware.  The pattern is
    rasterised on demand and cached per resolution, so changing the
    camera's requested size re-renders exactly once.
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
        self._frame: np.ndarray | None = None
        self._frame_resolution: tuple[int, int] | None = None
        self._ratio: float | None = None
        self._ratio_parsed = False
        self._open = False

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
            If the named pattern is not bundled with the package.
        """
        name = url.target or DEFAULT_IMAGE
        available = test_images()
        if name not in available:
            raise SourceError(
                f"Unknown test image {name!r}. "
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
        return SourceCapabilities(vendor="SpiriCamera", model=f"testimage:{self._image_name}")

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
        if self._frame is not None and self._frame_resolution == resolution:
            return self._frame

        self._frame = self._render(resolution)
        self._frame_resolution = resolution
        logger.debug(f"{self!r} rendered at {resolution[0]}x{resolution[1]}")
        return self._frame

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _aspect_ratio(self) -> float | None:
        """The pattern's intrinsic ratio, parsed once and remembered."""
        if not self._ratio_parsed:
            self._ratio = svg_aspect_ratio(test_images().get(self._image_name, ""))
            self._ratio_parsed = True
            logger.debug(f"{self!r} intrinsic aspect ratio: {self._ratio}")
        return self._ratio

    def _render(self, resolution: tuple[int, int]) -> np.ndarray:
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

    def _invalidate(self) -> None:
        """Drop the cached render so the next read re-rasterises."""
        self._frame = None
        self._frame_resolution = None
