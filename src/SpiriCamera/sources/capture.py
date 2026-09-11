"""Shared ``cv2.VideoCapture`` plumbing for device-backed sources.

Both the local V4L2 handler and the network stream handler are thin
wrappers around OpenCV's capture object.  Everything they have in
common — opening, applying requested settings, reading back what the
device negotiated, and grabbing frames — lives here.
"""

from __future__ import annotations

import abc

import cv2
import numpy as np
from loguru import logger

from SpiriCamera.sources.base import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
)


class OpenCVSource(SourceBase):
    """Base for sources backed by ``cv2.VideoCapture``.

    Subclasses supply the capture target via :py:meth:`capture_target`
    and may extend :py:meth:`device_metadata` to report vendor, model,
    and serial number.
    """

    def __init__(self, url) -> None:  # noqa: ANN001 - inherited signature
        super().__init__(url)
        self._capture: cv2.VideoCapture | None = None

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def capture_target(self) -> str | int:
        """Return the argument to hand to ``cv2.VideoCapture``.

        Returns
        -------
        str | int
            A device path, stream URL, or numeric camera index.
        """

    def device_metadata(self) -> dict[str, str]:
        """Look up vendor, model, and serial number for the open device.

        The default implementation reports nothing.  Subclasses override
        when their transport exposes device identity.

        Returns
        -------
        dict[str, str]
            Any of ``vendor``, ``model``, and ``serial_number``.
        """
        return {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Whether the capture object is open."""
        return self._capture is not None and self._capture.isOpened()

    def open(self, settings: CaptureSettings) -> SourceCapabilities:
        """Open the capture device and negotiate settings.

        Parameters
        ----------
        settings : CaptureSettings
            Requested resolution and framerate.

        Returns
        -------
        SourceCapabilities
            What the device actually negotiated.

        Raises
        ------
        SourceError
            If the device cannot be opened.
        """
        if self.is_open:
            logger.debug(f"{self!r} already open, re-reporting capabilities")
            return self._negotiated_capabilities()

        target = self.capture_target()
        logger.debug(f"{self!r} opening capture target {target!r}")

        capture = cv2.VideoCapture(target)
        if not capture.isOpened():
            capture.release()
            raise SourceError(f"Failed to open capture source: {self.url.raw!r}")

        self._capture = capture
        self._apply(settings)

        capabilities = self._negotiated_capabilities()
        logger.info(
            f"{self!r} opened | "
            f"{capabilities.max_supported_width}x{capabilities.max_supported_height} "
            f"@ {capabilities.max_supported_framerate}fps"
        )
        return capabilities

    def close(self) -> None:
        """Release the capture device."""
        if self._capture is not None:
            self._capture.release()
            self._capture = None
            logger.debug(f"{self!r} closed")

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        """Grab a single BGR frame from the device.

        Parameters
        ----------
        settings : CaptureSettings
            Requested capture settings; unused here because the device
            was configured at open time.

        Returns
        -------
        np.ndarray | None
            The frame, or ``None`` if the device produced nothing.

        Raises
        ------
        SourceError
            If the source was never opened.
        """
        if self._capture is None:
            raise SourceError(f"{self!r} is not open; call open() before read()")

        ok, frame = self._capture.read()
        if not ok or frame is None:
            return None
        return frame

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply(self, settings: CaptureSettings) -> None:
        """Push requested settings onto the device, which may clamp them."""
        assert self._capture is not None
        if settings.max_width > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, settings.max_width)
        if settings.max_height > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.max_height)
        if settings.max_framerate > 0:
            self._capture.set(cv2.CAP_PROP_FPS, settings.max_framerate)

    def _negotiated_capabilities(self) -> SourceCapabilities:
        """Read back what the device settled on, plus device identity."""
        assert self._capture is not None
        width = int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        framerate = int(self._capture.get(cv2.CAP_PROP_FPS) or 0)

        metadata = self.device_metadata()
        return SourceCapabilities(
            max_supported_width=width or None,
            max_supported_height=height or None,
            max_supported_framerate=framerate or None,
            vendor=metadata.get("vendor", ""),
            model=metadata.get("model", ""),
            serial_number=metadata.get("serial_number", ""),
        )
