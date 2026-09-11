"""V4L2 camera source handler."""

from __future__ import annotations

import cv2
from loguru import logger

from SpiriCamera.camera import Camera
from SpiriCamera.sources.base import SourceBase


class V4LSource(SourceBase):
    """V4L2 camera source handler.

    Handles /dev/videoN device paths and bare numeric indices.
    Automatically detected when source string matches V4L2 patterns.
    """

    protocols = ("v4l",)

    def __init__(self, path: str, camera_index: int) -> None:
        """Create a V4L2 source handler.

        Parameters
        ----------
        path : str
            Device path or numeric index.
        camera_index : int
            Numeric camera index.
        """
        self._path = path
        self._camera_index = camera_index
        self._capture: cv2.VideoCapture | None = None

    def start(self, camera: Camera) -> None:
        """Open the V4L2 capture device and detect capabilities.

        Parameters
        ----------
        camera : Camera
            Camera instance to configure.

        Raises
        ------
        RuntimeError
            If the capture device cannot be opened.
        """
        if self._capture is not None and self._capture.isOpened():
            logger.info("V4L source already running on %s", camera.synq_topic)
            return

        self._capture = cv2.VideoCapture(self._path)
        assert self._capture is not None

        if not self._capture.isOpened():
            self._capture = None
            raise RuntimeError(f"Failed to open V4L2 camera source: {self._path!r}")

        # Apply user-requested caps (OpenCV will clamp silently)
        if camera.max_width > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, camera.max_width)
        if camera.max_height > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, camera.max_height)
        if camera.max_framerate > 0:
            self._capture.set(cv2.CAP_PROP_FPS, camera.max_framerate)

        # Detect capabilities
        self._detect_capabilities(camera)

        # Attempt to populate vendor/model/serial
        self._extract_device_info(camera)

        logger.info(
            "V4L camera started | source=%s resolution=%dx%d fps=%s",
            self._path,
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
        logger.debug("V4L camera stopped on %s", camera.synq_topic)

    def read(self, camera: Camera) -> bytes:
        """Read a single frame from the V4L2 device and encode as JPEG.

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
                "V4L source not started. Call start() before read()."
            )

        ret, frame = self._capture.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to grab frame from V4L2 device")

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

    def _extract_device_info(self, camera: Camera) -> None:
        """Attempt to populate vendor information from V4L2 properties.

        Parameters
        ----------
        camera : Camera
            Camera instance to populate vendor/model fields.
        """
        assert self._capture is not None
        backend = self._capture.get(cv2.CAP_PROP_BACKEND)
        if backend and not camera.vendor:
            camera.vendor = "v4l2"
