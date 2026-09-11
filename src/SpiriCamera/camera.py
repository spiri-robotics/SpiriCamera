"""Camera source parsing and camera frame acquisition."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from SpiriSynq.syncable_objects import SyncableObject

import numpy as np

if TYPE_CHECKING:
    from SpiriCamera.sources import SourceBase


# ---------------------------------------------------------------------------
# Test images
# ---------------------------------------------------------------------------

_TEST_IMAGES_DIR = Path(__file__).parent / "test_images"


def _load_test_images() -> dict[str, str]:
    """Load all test SVG images from the test_images directory.

    Returns
    -------
    dict[str, str]
        Mapping of image name to SVG content.
    """
    test_images: dict[str, str] = {}
    for svg_file in _TEST_IMAGES_DIR.glob("*.svg"):
        test_images[svg_file.stem] = svg_file.read_text(encoding="utf-8")
    return test_images


TEST_IMAGES: dict[str, str] = _load_test_images()


# ---------------------------------------------------------------------------
# CameraSource
# ---------------------------------------------------------------------------


@dataclass
class CameraSource:
    """Parsed representation of a camera source URL.

    Holds scheme, path, and type information parsed from the original
    source string.  The actual source handler is instantiated by
    :py:func:`SpiriCamera.sources.discover`.
    """

    source_type: str
    scheme: str
    path: str
    camera_index: int | None = None
    is_valid: bool = True
    source: str = ""
    source_str: str = ""
    mimetype: str = "image/jpeg"
    max_supported_width: int | None = None
    max_supported_height: int | None = None
    max_supported_framerate: int | None = None
    max_width: int = 1920
    max_height: int = 1080
    max_framerate: int = 30
    quality: int = 80
    vendor: str = ""
    model: str = ""
    serial_number: str = ""
    image: bytes = b""
    synq_topic: str = ""

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse(source: str) -> CameraSource:
        """Parse a bare source string into a :py:class:`CameraSource`.

        Parameters
        ----------
        source : str
            The source string to parse (e.g. ``"/dev/video0"``,
            ``"rtsp://host/stream"``, ``"testimage://pm5544"``).

        Returns
        -------
        CameraSource
            A parsed representation of the source.

        Raises
        ------
        ValueError
            If the source string is empty or unrecognised.
        """
        if not source or not source.strip():
            raise ValueError("Source string cannot be empty")

        source = source.strip()

        # 1. Test image sources
        if source.startswith("testimage://"):
            image_name = source.replace("testimage://", "").strip()
            if not image_name:
                image_name = "pm5544"
            if image_name not in TEST_IMAGES:
                available = ", ".join(TEST_IMAGES.keys())
                raise ValueError(
                    f"Unknown test image: {image_name!r}. "
                    f"Available: {available}"
                )
            return CameraSource(
                source_type="local",
                scheme="testimage",
                path=image_name,
                camera_index=None,
                source=source,
            )

        # 2. Local V4L2 device path: /dev/videoN
        v4l_m = re.match(r"^/dev/video(\d+)$", source)
        if v4l_m:
            idx = int(v4l_m.group(1))
            return CameraSource(
                source_type="local",
                scheme="v4l",
                path=source,
                camera_index=idx,
                source=source,
            )

        # 3. Bare numeric index: "0", "1", ...
        if source.isdigit() and int(source) >= 0:
            idx = int(source)
            return CameraSource(
                source_type="local",
                scheme="v4l",
                path=str(idx),
                camera_index=idx,
                source=source,
            )

        # 4. Network schemes
        network_schemes: dict[str, str] = {
            "rtsp://": "rtsp",
            "rtmp://": "rtmp",
            "http://": "http",
            "https://": "https",
        }

        for prefix, scheme in network_schemes.items():
            if source.startswith(prefix):
                return CameraSource(
                    source_type="network",
                    scheme=scheme,
                    path=source,
                    camera_index=None,
                    source=source,
                )

        # No match
        raise ValueError(
            f"Unrecognised source: {source!r}. "
            "Supported formats: /dev/videoN, numeric index, rtsp://, rtmp://, "
            "http://, https://, testimage:// "
        )


# ---------------------------------------------------------------------------
# CameraBase
# ---------------------------------------------------------------------------


@dataclass
class CameraBase(SyncableObject):
    """Network-transparent camera base.

    Holds identity and capability metadata for a Camera instance.
    """

    _source_str: str = ""
    vendor: str = ""
    model: str = ""
    serial_number: str = ""
    mimetype: str = "image/jpeg"
    max_supported_width: int | None = None
    max_supported_height: int | None = None
    max_supported_framerate: int | None = None
    max_width: int = 1920
    max_height: int = 1080
    max_framerate: int = 30
    quality: int = 80
    image: bytes = b""
    synq_topic: str = ""

    @property
    def source_str(self) -> str:
        """The current source string."""
        return self._source_str

    @source_str.setter
    def source_str(self, value: str) -> None:
        self._source_str = value


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------


class Camera(CameraBase):
    """Video camera reader backed by source handlers.

    The Camera class is a thin wrapper around a source handler
    (:py:class:`~SpiriCamera.sources.base.SourceBase`).  Most of the
    frame acquisition logic lives in the source handler, which is
    determined from the source string.

    Usage::

        cam = Camera("/dev/video0", quality=85)
        cam.start()          # opens device, detects caps
        frame_bytes = cam.read()   # JPEG bytes
        cam.stop()

    For test images::

        cam = Camera("testimage://", max_width=1280, max_height=720)
        cam.start()
        frame_bytes = cam.read()   # renders SVG at 1280x720

    Environment variables (when launched from Docker or CLI)::

        SPIRICAMERA_SOURCE       Camera source string
        SPIRICAMERA_QUALITY      JPEG quality 1-100 (default 80)
        SPIRICAMERA_WIDTH        Capture width  (user override)
        SPIRICAMERA_HEIGHT       Capture height (user override)
        SPIRICAMERA_FRAMERATE    Capture framerate (user override)
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(
        self,
        source: str = "",
        quality: int = 80,
        max_width: int = 1920,
        max_height: int = 1080,
        max_framerate: int = 30,
    ) -> None:
        """Create a camera reader.

        Parameters
        ----------
        source : str
            Source URL or device path.
        quality : int
            JPEG quality of encoded frames, 1-100.
        max_width : int
            User-requested capture width.
        max_height : int
            User-requested capture height.
        max_framerate : int
            User-requested capture framerate.
        """
        safe_source = (
            source.lstrip("/")
            .replace("/", "_")
            .replace("://", "_")
            .replace(":", "_")
            if source
            else "camera"
        )
        synq_topic = f"spiricamera_{safe_source}"

        CameraBase.__init__(
            self,
            _source_str=source,
            mimetype="image/jpeg",
            max_width=max_width,
            max_height=max_height,
            max_framerate=max_framerate,
            quality=quality,
            synq_topic=synq_topic,
            synq_auto_start=False,
        )

        # Source handler
        self._source: SourceBase = None  # type: ignore[assignment]

        # Parse the source
        self._source_str = source
        source_obj, source_inst = self._reload_source(source)
        self._source_info = source_obj
        self._source = source_inst

        # Sync if enabled at class level
        if self.synq_auto_start:
            self.sync()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def source(self) -> CameraSource:
        """The parsed source description."""
        return self._source_info

    @property
    def source_str(self) -> str:
        """The current source string."""
        return self._source_str

    @source_str.setter
    def source_str(self, value: str) -> None:
        """Change the source and restart with the new source.

        Stops the current source, re-parses and instantiates the
        appropriate handler, and auto-starts it.

        Parameters
        ----------
        value : str
            The new source string.
        """
        self.stop()
        self._source_str = value
        source_obj, source_inst = self._reload_source(value)
        self._source_info = source_obj
        self._source = source_inst
        self.start()

    @property
    def running(self) -> bool:
        """Whether the source is currently active."""
        return getattr(self, "_running", False)

    @running.setter
    def running(self, value: bool) -> None:
        self._running = value

    # ------------------------------------------------------------------
    # Delegating properties for tests
    # ------------------------------------------------------------------

    @property
    def _capture(self) -> Any:
        """Delegate to the underlying source (V4L / Network)."""
        if self._source is not None:
            return getattr(self._source, "_capture", None)
        return None

    @_capture.setter
    def _capture(self, value: Any) -> None:
        """Set the underlying capture object (for testing)."""
        if self._source is not None:
            self._source._capture = value

    @property
    def _test_image_name(self) -> str | None:
        """Delegate to the underlying source (TestImage)."""
        if self._source is not None:
            return getattr(self._source, "_image_name", None)
        return None

    @property
    def _test_jpeg_cache(self) -> bytes | None:
        """Delegate to the underlying source (TestImage)."""
        if self._source is not None:
            return getattr(self._source, "_test_jpeg_cache", None)
        return None

    @_test_jpeg_cache.setter
    def _test_jpeg_cache(self, value: bytes | None) -> None:
        """Set the JPEG cache (for testing)."""
        if self._source is not None:
            self._source._test_jpeg_cache = value

    @property
    def _test_cache_resolution(self) -> tuple[int, int] | None:
        """Delegate to the underlying source (TestImage)."""
        if self._source is not None:
            return getattr(self._source, "_test_cache_resolution", None)
        return None

    @_test_cache_resolution.setter
    def _test_cache_resolution(self, value: tuple[int, int] | None) -> None:
        """Set the cache resolution (for testing)."""
        if self._source is not None:
            self._source._test_cache_resolution = value

    # ------------------------------------------------------------------
    # Source loading
    # ------------------------------------------------------------------

    def _reload_source(self, source: str) -> tuple[CameraSource, SourceBase]:
        """Reload the source handler for a source string.

        Parameters
        ----------
        source : str
            The source string to load.

        Returns
        -------
        tuple[CameraSource, SourceBase]
            The parsed info object and the source handler instance.
        """
        from SpiriCamera.sources import discover  # noqa: PLC0415, E402

        return discover(source)

    # ------------------------------------------------------------------
    # Static methods
    # ------------------------------------------------------------------

    @staticmethod
    def validate_source(source: str) -> CameraSource:
        """Validate a source string without creating a Camera instance.

        Parameters
        ----------
        source : str
            The source string to validate.

        Returns
        -------
        CameraSource
            The parsed source object.

        Raises
        ------
        ValueError
            If the source string is not valid.
        """
        from SpiriCamera.sources import validate_source  # noqa: PLC0415, E402

        return validate_source(source)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the capture device and detect device capabilities.

        Delegates to the underlying source handler, which populates
        :py:attr:`max_supported_width`, :py:attr:`max_supported_height`,
        and :py:attr:`max_supported_framerate`.
        """
        if self.running:
            logger.info("Camera {} already running, skipping start", self.synq_topic)
            return

        # Check if source is already open (V4L / Network)
        capture = getattr(self._source, "_capture", None)
        if capture is not None and capture.isOpened():
            logger.info("Camera {} already running, skipping start", self.synq_topic)
            return

        if not self.source.path:
            raise ValueError("No source path configured")

        self._source.start(self)
        self.running = True

        # Start Zenoh sync if we disabled auto-start during init
        if not self.synq_auto_start:
            self.synq_auto_start = True
            self.sync()

        logger.info(
            "Camera {} started | source={} resolution={}x{} fps={}",
            self.synq_topic,
            self.source.scheme,
            self.max_supported_width,
            self.max_supported_height,
            self.max_supported_framerate,
        )

    def stop(self) -> None:
        """Release the capture device and clean up the source."""
        if self._source is not None:
            self._source.stop(self)
        self.running = False
        logger.debug("Camera {} stopped", self.synq_topic)

    # ------------------------------------------------------------------
    # Frame reading
    # ------------------------------------------------------------------

    def read(self) -> bytes:
        """Grab a single frame and return it as JPEG bytes.

        Delegates to the underlying source handler, which is responsible
        for reading the frame, encoding it to JPEG, and setting
        :py:attr:`Camera.image`.

        Returns
        -------
        bytes
            JPEG-encoded frame data.

        Raises
        ------
        RuntimeError
            If the camera has not been started via :py:meth:`start`.
        """
        if not self.running:
            raise RuntimeError(
                "Camera not started. Call start() before read()."
            )

        if self._source is None:
            raise RuntimeError(
                "Camera not started. Call start() before read()."
            )

        return self._source.read(self)

    def read_numpy(self) -> np.ndarray | None:
        """Grab a single frame and return it as an OpenCV numpy array.

        Only meaningful for V4L2 sources.  Returns ``None`` for
        test image and network sources (or returns whatever the
        underlying source provides).

        Returns
        -------
        np.ndarray | None
            The frame as a BGR ``numpy`` array, or ``None`` on failure.
        """
        if not self.running:
            raise RuntimeError(
                "Camera not started. Call start() before read()."
            )

        if self._source is None:
            return None

        if self.source.scheme == "testimage":
            return None

        if self._capture is None:
            return None

        ret, frame = self._capture.read()
        if not ret or frame is None:
            return None

        return frame
