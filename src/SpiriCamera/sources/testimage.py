"""Test image source handler."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import resvg_py
from loguru import logger

from SpiriCamera.camera import TEST_IMAGES, Camera
from SpiriCamera.sources.base import SourceBase


class TestImageSource(SourceBase):
    """Test image source handler.

    Renders SVG test images to JPEG on demand at the requested
    resolution. Caches results per resolution to avoid unnecessary re-renders.
    """

    protocols = ("testimage://",)

    def __init__(self, image_name: str) -> None:
        """Create a test image source handler.

        Parameters
        ----------
        image_name : str
            Name of the test image (e.g. 'pm5544').
        """
        self._image_name = image_name
        self._test_jpeg_cache: bytes | None = None
        self._test_cache_resolution: tuple[int, int] | None = None

    def start(self, camera: Camera) -> None:
        """Start the test image source.

        Sets max_supported_* to None (test images render at any resolution)
        and registers a resize handler for cache invalidation.

        Parameters
        ----------
        camera : Camera
            Camera instance to configure.
        """
        self._test_jpeg_cache = None
        self._test_cache_resolution = None

        camera.max_supported_width = None
        camera.max_supported_height = None
        camera.max_supported_framerate = None

        camera.events.max_width.connect(self._on_resize)
        camera.events.max_height.connect(self._on_resize)

        logger.info(f"Test image started | source={camera.synq_topic} image={self._image_name} render={camera.max_width}x{camera.max_height}")

    def stop(self, camera: Camera) -> None:
        """Stop the test image source and disconnect events.

        Parameters
        ----------
        camera : Camera
            Camera instance to clean up.
        """
        try:
            camera.events.max_width.disconnect(self._on_resize)
        except Exception:
            pass
        try:
            camera.events.max_height.disconnect(self._on_resize)
        except Exception:
            pass
        logger.debug(f"Test image stopped on {camera.synq_topic}")

    def read(self, camera: Camera) -> bytes:
        """Read or render a test image frame as JPEG bytes.

        Uses camera.max_width and camera.max_height to determine
        the render resolution. Caches results per resolution.

        Parameters
        ----------
        camera : Camera
            Camera instance providing max_width, max_height, quality.

        Returns
        -------
        bytes
            JPEG-encoded frame data.
        """
        cache_key = (camera.max_width, camera.max_height)
        if self._test_cache_resolution == cache_key and self._test_jpeg_cache is not None:
            return self._test_jpeg_cache

        svg_str = TEST_IMAGES.get(self._image_name)
        if svg_str is None:
            raise ValueError(
                f"Test image {self._image_name!r} not found. "
                f"Available: {', '.join(TEST_IMAGES.keys())}"
            )

        # Render SVG at current resolution
        png_bytes = resvg_py.svg_to_bytes(
            svg_string=svg_str,
            width=camera.max_width,
            height=camera.max_height,
        )

        # Convert PNG → JPEG
        png_arr = np.frombuffer(png_bytes, dtype=np.uint8)
        frame = cv2.imdecode(png_arr, cv2.IMREAD_COLOR)
        _, encoded = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, camera.quality]
        )
        jpeg_bytes = bytes(encoded.tobytes())

        self._test_cache_resolution = cache_key
        self._test_jpeg_cache = jpeg_bytes
        camera.image = jpeg_bytes
        return jpeg_bytes

    def _on_resize(self, event) -> None:
        """Invalidate JPEG cache when resolution changes."""
        self._test_jpeg_cache = None
        self._test_cache_resolution = None
        logger.debug("Test image re-render triggered by resolution change")
