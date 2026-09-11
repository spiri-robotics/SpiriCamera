"""The camera domain: capture lifecycle, encoding, and publication.

A :py:class:`Camera` owns *when* frames are taken, *how* they are
encoded, and *where* they are published.  It owns none of the protocol
detail of getting hold of a frame — that belongs to the handlers in
:py:mod:`SpiriCamera.sources`, which hand back raw BGR arrays and never
reach back into the camera.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

import cv2
import numpy as np
from loguru import logger
from SpiriSynq.syncable_objects import SyncableObject

from SpiriCamera.sources import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceInfo,
    resolve_source,
)

#: Mimetypes the camera can encode to, mapped to an OpenCV extension.
ENCODINGS: dict[str, str] = {"image/jpeg": ".jpg", "image/png": ".png"}

#: How long :py:meth:`Camera.stop` waits for the capture thread to exit.
_STOP_TIMEOUT = 5.0

#: Log every Nth consecutive capture failure, so a dead source does not
#: fill the log at the full framerate.
_ERROR_LOG_INTERVAL = 60


class CameraError(RuntimeError):
    """Base class for camera-level failures."""


class CameraNotStartedError(CameraError):
    """Raised when frames are requested from a camera that is not running."""


class FrameUnavailableError(CameraError):
    """Raised when no frame could be produced and none was captured earlier."""


def topic_for_source(source_str: str) -> str:
    """Derive a stable SpiriSynq topic from a source string.

    Parameters
    ----------
    source_str : str
        The camera's initial source string.

    Returns
    -------
    str
        A topic-safe name such as ``spiricamera_dev_video0``.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", source_str).strip("_").lower()
    return f"spiricamera_{slug or 'camera'}"


# ---------------------------------------------------------------------------
# Synced state
# ---------------------------------------------------------------------------


@dataclass
class CameraBase(SyncableObject):
    """The network-transparent half of a camera.

    Every field here is published over SpiriSynq and can be set by a
    remote peer.  :py:class:`Camera` adds the behaviour that reacts to
    those fields; this class is what a mirror on another node sees.

    The fields fall into four groups:

    ``source_str`` / ``source``
        What the camera is pointed at.  ``source_str`` is the request;
        ``source`` is the resolved description, including any error.
    ``vendor`` / ``model`` / ``serial_number``
        Device identity, filled in by the source when it can.
    ``max_supported_*``
        What the source reported when it last opened.  Writable like
        everything else here — nothing in SpiriSynq is truly read-only —
        though a value set by hand does not reconfigure the device and
        is overwritten the next time the source opens.
    ``received_*``
        The dimensions of the frame that actually arrived, measured off
        the last frame rather than asked for or advertised.  A source
        may legitimately return something smaller than ``max_width`` x
        ``max_height``, or a different shape than either, so this is the
        only group that says what is really being encoded.
    ``max_width`` / ``max_height`` / ``max_framerate`` / ``quality`` / ``mimetype``
        The requested capture and encoding settings.  These are live:
        changing them takes effect on the next frame.

    .. note::

       No field here is read-only, and none should be made read-only.
       Every one of them is reachable over SpiriSynq, so a remote peer
       can already write any of them; a property setter or a one-way UI
       binding would only hide that, not prevent it.  Some fields are
       *reported* rather than *requested* — the ``max_supported_*``
       group especially — but "the source overwrites this on open" is a
       description of behaviour, not a contract that it cannot be
       written.  Being able to set a field to a deliberately wrong
       value and watch what downstream does with it is a debugging
       feature worth keeping.

    Attributes
    ----------
    source_str : str
        Requested source URL.  Assigning to it retargets the camera.
    source : SourceInfo
        Resolved description of :py:attr:`source_str`.
    running : bool
        Whether capture is active.  Assigning to it starts or stops the
        camera, including from a remote peer.
    image : bytes
        The most recent encoded frame, in :py:attr:`mimetype` format.
    """

    source_str: str = ""
    source: SourceInfo = field(default_factory=SourceInfo)

    vendor: str = ""
    model: str = ""
    serial_number: str = ""

    max_supported_width: int | None = None
    max_supported_height: int | None = None
    max_supported_framerate: int | None = None

    received_width: int = 0
    received_height: int = 0
    received_ratio: float = 0.0

    max_width: int = 1920
    max_height: int = 1080
    max_framerate: int = 30
    quality: int = 80
    mimetype: str = "image/jpeg"

    running: bool = False
    image: bytes = b""


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


class Camera(CameraBase):
    """A live camera: resolves a source, captures frames, publishes them.

    Usage::

        cam = Camera("/dev/video0", quality=85)
        cam.start()
        frame_bytes = cam.read()    # encoded bytes, also on cam.image
        cam.stop()

    or as a context manager::

        with Camera("testimage://", max_width=1280, max_height=720) as cam:
            frame_bytes = cam.read()

    While running, a background thread captures at
    :py:attr:`~CameraBase.max_framerate` and assigns each encoded frame
    to :py:attr:`~CameraBase.image`, which publishes it to SpiriSynq.
    Settings are live: changing ``max_width``, ``quality``, or
    ``max_framerate`` — locally or from a remote peer — takes effect on
    the next frame without a restart.

    An unusable source is not fatal.  Construction never raises for a
    bad source string; the reason lands in ``camera.source.error`` and
    :py:meth:`start` is what refuses.  This keeps the camera bindable to
    a live-edited input field.
    """

    def __init__(
        self,
        source: str = "",
        *,
        quality: int = 80,
        max_width: int = 1920,
        max_height: int = 1080,
        max_framerate: int = 30,
        mimetype: str = "image/jpeg",
        synq_topic: str = "",
        synq_auto_start: bool = True,
    ) -> None:
        """Create a camera.

        Parameters
        ----------
        source : str
            Source URL or device path.  May be empty or invalid; see the
            class docstring.
        quality : int
            Encode quality for JPEG frames, 1-100.
        max_width : int
            Requested capture width.
        max_height : int
            Requested capture height.
        max_framerate : int
            Requested capture framerate, and the rate of the capture
            thread.
        mimetype : str
            Encoding for :py:attr:`~CameraBase.image`; see
            :py:data:`ENCODINGS`.
        synq_topic : str
            SpiriSynq topic.  Defaults to one derived from ``source``,
            and is fixed for the lifetime of the object even if the
            source later changes.
        synq_auto_start : bool
            Whether to join the SpiriSynq session immediately.
        """
        CameraBase.__init__(
            self,
            synq_topic=synq_topic or topic_for_source(source),
            source_str=source,
            quality=quality,
            max_width=max_width,
            max_height=max_height,
            max_framerate=max_framerate,
            mimetype=mimetype,
            # Deferred: the object is not wired up enough to sync yet.
            synq_auto_start=False,
        )

        self._lock = threading.RLock()
        self._in_lifecycle = False
        self._active = False
        self._handler: SourceBase | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._cached_frame: np.ndarray | None = None
        self._cached_encoding: tuple[str, int] | None = None
        self._cached_image: bytes = b""

        self._resolve(source)

        # React to our own state changing, whoever changed it — a local
        # caller, a UI binding, or a remote peer over SpiriSynq.
        self.events.source_str.connect(self._on_source_str_changed)
        self.events.running.connect(self._on_running_changed)

        self.synq_auto_start = synq_auto_start
        if synq_auto_start:
            self.sync()

    # ------------------------------------------------------------------
    # Source resolution
    # ------------------------------------------------------------------

    @property
    def handler(self) -> SourceBase | None:
        """The resolved source handler, or ``None`` if unresolvable."""
        return self._handler

    def _resolve(self, source_str: str) -> None:
        """Resolve a source string into a handler, recording any failure.

        Never raises: an unusable source leaves :py:attr:`handler` as
        ``None`` and puts the reason in ``self.source.error``.
        """
        if not source_str or not source_str.strip():
            self._handler = None
            self.source.update(
                SourceInfo.unresolved(source_str, "No source configured")
            )
            return

        try:
            handler = resolve_source(source_str)
        except SourceError as exc:
            self._handler = None
            self.source.update(SourceInfo.unresolved(source_str, str(exc)))
            logger.warning(f"{self.synq_topic}: cannot resolve {source_str!r}: {exc}")
            return

        self._handler = handler
        self.source.update(handler.describe())
        logger.debug(f"{self.synq_topic}: resolved {source_str!r} to {handler!r}")

    def _on_source_str_changed(self, source_str: str) -> None:
        """Retarget the camera when the source string changes."""
        was_running = self._active
        self.stop()
        self._resolve(source_str)
        if was_running and self._handler is not None:
            try:
                self.start()
            except CameraError as exc:
                logger.warning(f"{self.synq_topic}: restart after retarget failed: {exc}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def _lifecycle(self) -> Generator[None]:
        """Hold the lifecycle lock and suppress our own ``running`` event."""
        with self._lock:
            previous = self._in_lifecycle
            self._in_lifecycle = True
            try:
                yield
            finally:
                self._in_lifecycle = previous

    def start(self, *, background: bool = True) -> None:
        """Open the source and begin capturing.

        Starting an already-running camera is a no-op.

        Parameters
        ----------
        background : bool
            Whether to run a capture thread.  With ``False`` the camera
            opens but captures nothing until the caller drives
            :py:meth:`read` — useful for scripted, frame-at-a-time use.

        Raises
        ------
        CameraError
            If the source string could not be resolved, or the source
            refused to open.
        """
        with self._lifecycle():
            if self._active:
                logger.debug(f"{self.synq_topic}: already running")
                return

            if self._handler is None:
                raise CameraError(
                    f"{self.synq_topic}: cannot start, "
                    f"{self.source.error or 'no source configured'}"
                )

            try:
                capabilities = self._handler.open(self.capture_settings())
            except SourceError as exc:
                self._handler.close()
                raise CameraError(f"{self.synq_topic}: {exc}") from exc

            self._apply(capabilities)
            self._stop_event.clear()
            self._active = True
            self.running = True

            if background:
                self._thread = threading.Thread(
                    target=self._capture_loop,
                    name=f"spiricamera-{self.synq_topic}",
                    daemon=True,
                )
                self._thread.start()

        logger.info(
            f"{self.synq_topic} started | {self.source.url} | "
            f"{self.describe_capabilities()}"
        )

    def stop(self) -> None:
        """Stop capturing and release the source.

        Stopping an already-stopped camera is a no-op.  Safe to call
        from the capture thread itself, in which case the thread is not
        joined.
        """
        with self._lifecycle():
            if not self._active and self._thread is None and not self._is_source_open():
                return
            self._stop_event.set()
            thread, self._thread = self._thread, None
            self._active = False
            self.running = False

        # Joining outside the lock: the capture thread never takes it,
        # but the source must not be closed while a read is in flight.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_STOP_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    f"{self.synq_topic}: capture thread did not exit within "
                    f"{_STOP_TIMEOUT}s"
                )

        with self._lock:
            if self._handler is not None:
                self._handler.close()
            self._cached_frame = None
            self._cached_encoding = None
            self._cached_image = b""

        logger.debug(f"{self.synq_topic} stopped")

    def _is_source_open(self) -> bool:
        """Whether the handler still holds an open device."""
        return self._handler is not None and self._handler.is_open

    def _on_running_changed(self, running: bool) -> None:
        """Start or stop when ``running`` is set by someone else."""
        if self._in_lifecycle:
            return
        try:
            self.start() if running else self.stop()
        except CameraError as exc:
            logger.warning(f"{self.synq_topic}: {exc}")
            with self._lifecycle():
                self.running = False

    def close(self) -> None:
        """Stop the camera and release its SpiriSynq resources."""
        try:
            self.stop()
        except Exception as exc:  # pragma: no cover - shutdown best effort
            logger.debug(f"{self.synq_topic}: error while stopping: {exc}")
        super().close()

    def __enter__(self) -> Camera:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture_settings(self) -> CaptureSettings:
        """Snapshot the currently requested capture settings.

        Values arriving from UI bindings or remote peers may be floats or
        ``None``; they are coerced to positive integers here so sources
        can trust what they are given.

        Returns
        -------
        CaptureSettings
            The settings to hand to the source.
        """
        return CaptureSettings(
            max_width=_positive(self.max_width),
            max_height=_positive(self.max_height),
            max_framerate=_positive(self.max_framerate),
        )

    def describe_capabilities(self) -> str:
        """Summarise what the source reported, for logs and the CLI.

        Returns
        -------
        str
            Something like ``1920x1080 @ 30fps``.  Sources that render
            at whatever size they are asked for report no fixed limits,
            and are described as ``any size`` / ``any rate``.
        """
        if self.max_supported_width and self.max_supported_height:
            size = f"{self.max_supported_width}x{self.max_supported_height}"
        else:
            size = "any size"
        rate = (
            f"{self.max_supported_framerate}fps"
            if self.max_supported_framerate
            else "any rate"
        )
        return f"{size} @ {rate}"

    def read_frame(self) -> np.ndarray | None:
        """Grab one raw frame from the source.

        Returns
        -------
        np.ndarray | None
            A BGR frame, or ``None`` if the source had nothing new.

        Raises
        ------
        CameraNotStartedError
            If the camera is not running.
        SourceError
            If the source failed unrecoverably.
        """
        handler = self._handler
        if not self._active or handler is None:
            raise CameraNotStartedError(
                f"{self.synq_topic}: not started, call start() first"
            )
        return handler.read(self.capture_settings())

    def read(self) -> bytes:
        """Capture, encode, and publish a single frame.

        The encoded frame is assigned to :py:attr:`~CameraBase.image`,
        which is what publishes it to SpiriSynq.

        Returns
        -------
        bytes
            The encoded frame, in :py:attr:`~CameraBase.mimetype` format.
            If the source had no new frame, the previous one is returned
            unchanged.

        Raises
        ------
        CameraNotStartedError
            If the camera is not running.
        FrameUnavailableError
            If the source produced nothing and no earlier frame exists.
        """
        frame = self.read_frame()
        if frame is None:
            if not self.image:
                raise FrameUnavailableError(
                    f"{self.synq_topic}: source produced no frame"
                )
            return self.image

        self._note_received(frame)
        encoded = self._encode(frame)
        self.image = encoded
        return encoded

    def _capture_loop(self) -> None:
        """Capture at ``max_framerate`` until stopped.

        Settings are re-read every iteration, so a live change to the
        framerate applies without restarting the camera.
        """
        failures = 0
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self.read()
                failures = 0
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % _ERROR_LOG_INTERVAL == 0:
                    logger.warning(
                        f"{self.synq_topic}: capture failed "
                        f"({failures} in a row): {exc}"
                    )

            interval = 1.0 / _positive(self.max_framerate)
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

        logger.debug(f"{self.synq_topic}: capture loop exited")

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    def _encode(self, frame: np.ndarray) -> bytes:
        """Encode a BGR frame to :py:attr:`~CameraBase.mimetype` bytes.

        A source that keeps handing back the same array — a static test
        pattern, say — is encoded once and served from cache until the
        frame or the encoding settings change.
        """
        quality = max(1, min(100, _positive(self.quality)))
        encoding = (self.mimetype, quality)

        if self._cached_frame is frame and self._cached_encoding == encoding:
            return self._cached_image

        extension = ENCODINGS.get(self.mimetype)
        if extension is None:
            raise CameraError(
                f"{self.synq_topic}: cannot encode {self.mimetype!r}, "
                f"supported: {', '.join(sorted(ENCODINGS))}"
            )

        params = [cv2.IMWRITE_JPEG_QUALITY, quality] if extension == ".jpg" else []
        ok, buffer = cv2.imencode(extension, frame, params)
        if not ok:
            raise CameraError(f"{self.synq_topic}: failed to encode frame")

        encoded = buffer.tobytes()
        self._cached_frame = frame
        self._cached_encoding = encoding
        self._cached_image = encoded
        return encoded

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _note_received(self, frame: np.ndarray) -> None:
        """Record the dimensions of the frame that actually arrived.

        Assignments only publish when the value really changed, so a
        steady stream at a fixed size costs nothing after the first
        frame.
        """
        height, width = frame.shape[:2]
        self.received_width = int(width)
        self.received_height = int(height)
        self.received_ratio = round(width / height, 4) if height else 0.0

    def _apply(self, capabilities: SourceCapabilities) -> None:
        """Copy what the source reported into the synced state.

        Identity fields are only overwritten when the source actually
        reported something, so a value set by hand survives a restart.
        """
        self.max_supported_width = capabilities.max_supported_width
        self.max_supported_height = capabilities.max_supported_height
        self.max_supported_framerate = capabilities.max_supported_framerate

        if capabilities.vendor:
            self.vendor = capabilities.vendor
        if capabilities.model:
            self.model = capabilities.model
        if capabilities.serial_number:
            self.serial_number = capabilities.serial_number


def _positive(value: object, default: int = 1) -> int:
    """Coerce a bound UI or remote value to a positive integer."""
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(default, number)
