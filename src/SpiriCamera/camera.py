"""Camera source parsing and camera frame acquisition."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
import resvg_py
from loguru import logger
from SpiriSynq.syncable_objects import SyncableObject

# ---------------------------------------------------------------------------
# Test images
# ---------------------------------------------------------------------------

_TEST_IMAGES_DIR = Path(__file__).parent / "test_images"

_TEST_IMAGES: dict[str, str] = {}


def _load_test_images() -> dict[str, str]:
    for svg_file in _TEST_IMAGES_DIR.glob("*.svg"):
        _TEST_IMAGES[svg_file.stem] = svg_file.read_text(encoding="utf-8")
    return _TEST_IMAGES


TEST_IMAGES: dict[str, str] = _load_test_images()


# ---------------------------------------------------------------------------
# CameraSource
# ---------------------------------------------------------------------------


@dataclass
class CameraSource:
    """Parsed representation of a camera source URL.

    Parses bare source strings (e.g. ``/dev/video0``, ``rtsp://host/stream``,
    ``"0"``, ``testimage://``) into a structured object that can be passed to a
    :py:class:`Camera`.

    Detection rules (no prefix required)::

        Path starting with ``/dev/video`` → type=local,    scheme=v4l
        ``rtsp://``                        → type=network,  scheme=rtsp
        ``rtmp://``                        → type=network,  scheme=rtmp
        ``http://`` or ``https://``        → type=network,  scheme=http(s)
        ``testimage://``                   → type=local,    scheme=testimage
        Digits only (e.g. ``"0"``)         → type=local,    scheme=v4l
        Anything else                      → ValueError

    Attributes
    ----------
    source_type : Literal["local", "network"]
        Whether the source is a local device or a network stream.
    scheme : Literal["v4l", "rtsp", "rtmp", "http", "https", "testimage"]
        The protocol / interface scheme detected from the source string.
    path : str
        The normalised path used directly with ``cv2.VideoCapture``, or the
        test-image identifier for testimage sources (``"pm5544"``).
    camera_index : int | None
        Numeric index for local V4L2 cameras (``0`` for ``/dev/video0``).
        ``None`` for network sources and test images.
    source : str
        The original unparsed source string.
    is_valid : bool
        ``True`` when the source is parseable and potentially supported by
        OpenCV.  ``False`` is returned when a heuristic heuristic suggests
        the source is unlikely to work (e.g. unsupported scheme).
    """

    source_type: Literal["local", "network"]
    scheme: Literal["v4l", "rtsp", "rtmp", "http", "https", "testimage"]
    path: str
    camera_index: int | None = None
    is_valid: bool = True
    source: str = ""

    @staticmethod
    def parse(source: str) -> CameraSource:
        """Parse a bare source string into a :py:class:`CameraSource`.

        Parameters
        ----------
        source : str
            The source string to parse.  Expected formats include::

                /dev/video0                    # V4L2 device
                /dev/video1                    # V4L2 device
                0                              # Numeric index
                rtsp://user:pass@host:554/stream
                rtmp://host/app/stream
                http://host/image.jpg
                https://host/image.jpg
                testimage://                   # Test image (default)
                testimage://pm5544             # Test image (named)

        Returns
        -------
        CameraSource
            A parsed representation of the source.

        Raises
        ------
        ValueError
            If the source string is empty or cannot be matched to a
            supported scheme.
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
        network_schemes: dict[str, Literal["rtsp", "rtmp", "http", "https"]] = {
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
    The :py:attr:`synq_topic` is the canonical identity — it doubles as
    the camera's name on the SpiriSynq network.

    Attributes
    ----------
    vendor : str
        Camera vendor (auto-detected when possible).
    model : str
        Camera model (auto-detected when possible).
    serial_number : str
        Camera serial number (auto-detected when possible).
    source_str : str
        Original source string (parsed by :py:class:`CameraSource`).
    mimetype : str
        Content type of captured frames (default ``image/jpeg``).
    max_supported_width : int
        Width supported by the device (auto-detected).
        ``None`` for test image sources.
    max_supported_height : int
        Height supported by the device (auto-detected).
        ``None`` for test image sources.
    max_supported_framerate : int
        Framerate supported by the device (auto-detected).
        ``None`` for test image sources.
    max_width : int
        User-requested capture width.
    max_height : int
        User-requested capture height.
    max_framerate : int
        User-requested capture framerate.
    quality : int
        JPEG quality for encoded frames [1-100].
    image : bytes
        Most recently captured JPEG frame data.
    synq_topic : str
        Zenoh sync topic for network synchronization; doubles as the
        canonical camera name (e.g. ``spiricamera//dev/video0``).
    """

    vendor: str = ""
    model: str = ""
    serial_number: str = ""
    source_str: str = ""
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


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------


class Camera(CameraBase):
    """Video camera reader backed by OpenCV.

    Inherits all identity and capability fields from :py:class:`CameraBase`
    and adds live frame acquisition via ``cv2.VideoCapture``.

    For ``testimage://`` sources, frames are rendered from an SVG using
    ``resvg_py`` at the configured width and height on every call to
    :py:meth:`read`.  Changes to ``max_width`` and ``max_height`` are caught
    via ``self.events`` and trigger a re-render automatically.

    Usage::

        cam = Camera("/dev/video0", quality=85)
        cam.start()          # opens capture, detects device caps
        frame_bytes = cam.read()   # JPEG bytes (updates self.image)
        cam.stop()

    For test images::

        cam = Camera("testimage://", max_width=1280, max_height=720)
        cam.start()
        frame_bytes = cam.read()   # renders SVG at 1280x720, JPEG encoded

    Environment variables (when launched from Docker or CLI)::

        SPIRICAMERA_SOURCE       Camera source string
        SPIRICAMERA_QUALITY      JPEG quality 1-100 (default 80)
        SPIRICAMERA_WIDTH        Capture width  (user override)
        SPIRICAMERA_HEIGHT       Capture height (user override)
        SPIRICAMERA_FRAMERATE    Capture framerate (user override)

    Attributes
    ----------
    source : CameraSource
        The parsed source description.
    quality : int
        JPEG quality for encoded frames [1-100].
    image : bytes
        The most recently captured JPEG frame.
    running : bool
        ``True`` while the capture object is open.
    synq_topic : str
        The Zenoh sync topic; also the canonical camera name.
    """

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
            Source URL or device path (parsed by :py:class:`CameraSource`).
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
            source_str=source,
            mimetype="image/jpeg",
            max_width=max_width,
            max_height=max_height,
            max_framerate=max_framerate,
            quality=quality,
            synq_topic=synq_topic,
            synq_auto_start=False,
        )

        # Behavioural state
        self._capture: cv2.VideoCapture | None = None
        self.image: bytes = b""
        self.running = False

        # Test image state
        self._test_image_name: str = "pm5544"
        self._test_jpeg_cache: bytes | None = None
        self._test_cache_resolution: tuple[int, int] | None = None

        # Parse the source
        self._source_parsed: CameraSource = CameraSource.parse(source)

    @property
    def source(self) -> CameraSource:  # type: ignore[override]
        """The parsed source description (:py:class:`CameraSource`)."""
        return self._source_parsed

    # ------------------------------------------------------------------
    # Class methods
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
        return CameraSource.parse(source)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the video capture device and detect device capabilities.

        For test image sources, initialises test image rendering.  For
        real camera sources, opens a ``cv2.VideoCapture`` and probes
        hardware capabilities.

        Raises
        ------
        RuntimeError
            If the capture device cannot be opened (source is unavailable).
        ValueError
            If user-requested width / height / framerate are out of range.
        """
        if self.running or (self._capture is not None and self._capture.isOpened()):
            logger.info("Camera %s already running, skipping start", self.synq_topic)
            return

        if not self.source.path:
            raise ValueError("No source path configured")

        # Test image source
        if self.source.scheme == "testimage":
            self._start_test_image()
            self.running = True
            logger.info(
                "Camera %s started | source=%s (test image: %s)",
                self.synq_topic,
                self.source.scheme,
                self._test_image_name,
            )
            return

        # Real camera source
        self._capture = cv2.VideoCapture(self.source.path)
        assert self._capture is not None
        if not self._capture.isOpened():
            self._capture = None
            raise RuntimeError(
                f"Failed to open camera source: {self.source.source!r}"
            )

        # Apply user caps (if given; OpenCV will clamp silently)
        assert self._capture is not None
        if self.max_width > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.max_width)
        if self.max_height > 0:
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.max_height)
        if self.max_framerate > 0:
            self._capture.set(cv2.CAP_PROP_FPS, self.max_framerate)

        # Start Zenoh sync if we disabled auto-start during init
        if not self.synq_auto_start:
            self.synq_auto_start = True
            self.sync()

        # Auto-detect device capabilities
        self._detect_capabilities()

        self.running = True
        logger.info(
            "Camera %s started | source=%s resolution=%dx%d fps=%d",
            self.synq_topic,
            self.source.scheme,
            self.max_supported_width,
            self.max_supported_height,
            self.max_supported_framerate,
        )

    def _start_test_image(self) -> None:
        """Start a test image source."""
        self._test_image_name = self.source.path
        self._test_jpeg_cache = None
        self._test_cache_resolution = None

        # Set supported to None for test images (they render at any resolution)
        self.max_supported_width = None
        max_supported_height = None
        max_supported_framerate = None

        # Register resize handler to re-render when resolution changes
        self.events.max_width.connect(self._on_test_image_resize)
        self.events.max_height.connect(self._on_test_image_resize)

        # Start Zenoh sync
        if not self.synq_auto_start:
            self.synq_auto_start = True
            self.sync()

        self.running = True
        logger.info(
            "Camera %s started | source=%s (%s) test_image render at %dx%d",
            self.synq_topic,
            self.source.scheme,
            self._test_image_name,
            self.max_width,
            self.max_height,
        )

    def _on_test_image_resize(self, event) -> None:
        """Re-render test image when max_width or max_height changes."""
        self._test_jpeg_cache = None
        self._test_cache_resolution = None
        logger.debug(
            "Test image re-render triggered for %s at %dx%d",
            self.synq_topic,
            self.max_width,
            self.max_height,
        )

    def stop(self) -> None:
        """Release the capture device."""
        # Disconnect test image resize handlers
        try:
            self.events.max_width.disconnect(self._on_test_image_resize)
        except Exception:
            pass
        try:
            self.events.max_height.disconnect(self._on_test_image_resize)
        except Exception:
            pass

        if self._capture is not None:
            self._capture.release()
        self._capture = None
        self.running = False
        logger.debug("Camera %s stopped", self.synq_topic)

    # ------------------------------------------------------------------
    # Frame reading
    # ------------------------------------------------------------------

    def read(self) -> bytes:
        """Grab a single frame and return it as JPEG bytes.

        For real camera sources, reads from the capture device and encodes
        to JPEG.  For test image sources, renders the SVG at the current
        ``max_width`` × ``max_height`` resolution and encodes to JPEG.

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

        if self.source.scheme == "testimage":
            return self._read_test_image()

        if self._capture is None:
            raise RuntimeError(
                "Camera not started. Call start() before read()."
            )

        ret, frame = self._capture.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to grab frame from camera")

        _, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        self.image = bytes(encoded.tobytes())
        return self.image

    def _read_test_image(self) -> bytes:
        """Read or render a test image frame as JPEG bytes."""
        cache_key = (self.max_width, self.max_height)
        if self._test_cache_resolution == cache_key and self._test_jpeg_cache is not None:
            return self._test_jpeg_cache

        svg_str = TEST_IMAGES.get(self._test_image_name)
        if svg_str is None:
            raise ValueError(
                f"Test image {self._test_image_name!r} not found. "
                f"Available: {', '.join(TEST_IMAGES.keys())}"
            )

        # Render SVG at current resolution
        png_bytes = resvg_py.svg_to_bytes(
            svg_string=svg_str,
            width=self.max_width,
            height=self.max_height,
        )

        # Convert PNG → JPEG
        png_arr = np.frombuffer(png_bytes, dtype=np.uint8)
        frame = cv2.imdecode(png_arr, cv2.IMREAD_COLOR)
        _, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        jpeg_bytes = bytes(encoded.tobytes())

        self._test_cache_resolution = cache_key
        self._test_jpeg_cache = jpeg_bytes
        self.image = jpeg_bytes
        return jpeg_bytes

    def read_numpy(self) -> np.ndarray | None:
        """Grab a single frame and return it as an OpenCV numpy array.

        Returns
        -------
        np.ndarray | None
            The frame as a BGR ``numpy`` array, or ``None`` on failure.
        """
        if not self.running:
            raise RuntimeError(
                "Camera not started. Call start() before read()."
            )

        if self._capture is None:
            return None

        ret, frame = self._capture.read()
        if not ret or frame is None:
            return None

        return frame

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_capabilities(self) -> None:
        """Read device-supported resolution and framerate from the capture object."""
        if self._capture is None:
            return

        # OpenCV CAP_PROP_* returns the *set* value on some backends,
        # and the *actual* value on others.  Fall back to 0 when the
        # value is not available.
        w = self._capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
        h = self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0
        fps = self._capture.get(cv2.CAP_PROP_FPS) or 0

        self.max_supported_width = int(w)
        self.max_supported_height = int(h)
        self.max_supported_framerate = int(fps)

        # Attempt to pick up vendor / model / serial from V4L2 device
        # properties (not all drivers expose these).
        self._extract_device_info()

    def _extract_device_info(self) -> None:
        """Attempt to populate vendor / model / serial from V4L2 properties.

        Only meaningful for ``/dev/video*`` sources.
        """
        if self.source.scheme != "v4l":
            return

        assert self._capture is not None

        # CAP_PROP_BACKEND may expose driver name
        backend = self._capture.get(cv2.CAP_PROP_BACKEND)
        if backend and not self.vendor:
            # Map integer backend enum back to a string – this is
            # an approximation; V4L2 backend is the common case.
            self.vendor = "v4l2"

        # CAP_PROP_AUTOFOCUS, CAP_PROP_WHITE_BALANCE etc. are only
        # meaningful on specific hardware; skip for now.
        # Serial / model are typically unavailable for standard
        # V4L2 webcams.  A more advanced approach would use
        # ``/sys/class/video4linux/videoX/device/`` sysfs probing.
