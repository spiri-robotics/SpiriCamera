"""Network camera source handler (RTSP, RTMP, HTTP, HTTPS)."""

from __future__ import annotations

import cv2
from loguru import logger

from SpiriCamera.camera import Camera
from SpiriCamera.sources.base import SourceBase


class NetworkSource(SourceBase):
    """Network camera source handler.

    Handles RTSP, RTMP, HTTP, and HTTPS URLs. These are all opened
    via ``cv2.VideoCapture`` and share the same capability detection
    and frame reading logic.
    """

    protocols = ("rtsp://", "rtmp://", "http://", "https://")

    def __init__(self, path: str, scheme: str) -> None:
        """Create a network camera source handler.

        Parameters
        ----------
        path : str
            The full network URL.
        scheme : str
            The protocol scheme (rtsp, rtmp, http, https).
        """
        self._path = path
        self._scheme = scheme
        self._capture: cv2.VideoCapture | None = None

    def start(self, camera: Camera) -> None:
        """Open the network capture and detect capabilities.

        Parameters
        ----------
        camera : Camera
            Camera instance to configure and populate.

        Raises
        ------
        RuntimeError
            If the network stream cannot be opened.
        """
        if self._capture is not None and self._capture.isOpened():
            logger.info("Network source already running %s", camera.synq_topic)
            return

        self._capture = cv2.VideoCapture(self._path)
        assert self._capture is not None

        if not self._capture.isOpened():
            self._capture = None
            raise RuntimeError(
                f"Failed to open network camera source: {self._path!r}"
            )

        # Apply user-requested caps
        if camera.max_width > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, camera.max_width)
        if camera.max_height > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, camera.max_height)
        if camera.max_framerate > 0:
            self._capture.set(cv2.CAP_PROP_FPS, camera.max_framerate)

        # Detect capabilities
        self._detect_capabilities(camera)

        logger.info(
            "Network camera started | source=%s scheme=%s resolution=%dx%d fps=%s",
            self._path,
            self._scheme,
            camera.max_supported_width,
            camera.max_supported_height,
            camera.max_supported_framerate,
        )

    def stop(self, camera: Camera) -> None:
        """Release the capture device.

        Parameters
        ----------
        camera : Camera
            Camera instance to clean up.
        """
        if self._capture is not None:
            self._capture.release()
        self._capture = None
        logger.debug("Network camera stopped on %s", camera.synq_topic)

    def read(self, camera: Camera) -> bytes:
        """Read a single frame from the network stream and encode as JPEG.

        Parameters
        ----------
        camera : Camera
            Camera instance providing quality and other settings.

        Returns
        -------
        bytes
            JPEG-encoded frame data.

        Raises
        ------
        RuntimeError
            If no frame could be grabbed.
        """
        if self._capture is None:
            raise RuntimeError(
                "Network source not started. Call start() before read()."
            )

        ret, frame = self._capture.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to grab frame from network camera")

        _, encoded = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, camera.quality]
        )
        camera.image = bytes(encoded.tobytes())
        return camera.image

    def _detect_capabilities(self, camera: Camera) -> None:
        """Read device-supported resolution and framerate.

        Parameters
        ----------
        camera : Camera
            Camera instance to populate max_supported_* fields.
        """
        if self._capture is None:
            return

        w = self._capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
        h = self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0
        fps = self._capture.get(cv2.CAP_PROP_FPS) or 0

        camera.max_supported_width = int(w)
        camera.max_supported_height = int(h)
        camera.max_supported_framerate = int(fps)
