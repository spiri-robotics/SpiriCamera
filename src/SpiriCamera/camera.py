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

from SpiriCamera import exif
from SpiriCamera.sources import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceInfo,
    resolve_source,
)

#: Mimetypes the camera can encode to, mapped to an OpenCV extension.
#:
#: JPEG only.  It is the format every consumer of this package already
#: decodes, it is the only one the frame tags in :py:mod:`SpiriCamera.exif`
#: are written for, and offering a second one bought nothing but a second
#: path to test.
ENCODINGS: dict[str, str] = {"image/jpeg": ".jpg"}

#: :py:attr:`CameraBase.status` while frames are being captured.
STATUS_RUNNING = "running"

#: :py:attr:`CameraBase.status` while idle with nothing wrong.
STATUS_STOPPED = "stopped"

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

    The fields fall into five groups:

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
    ``max_width`` / ``max_height`` / ``max_framerate`` / ``quality`` / ``mimetype`` / ``exif_enabled`` / ``exif_extra``
        The requested capture, encoding and tagging settings.  These are
        live: changing them takes effect on the next frame.
    ``received_*`` / ``exif_tags`` / ``exif_timestamp``
        What the current :py:attr:`image` turned out to be.  See
        :py:data:`synq_skip_sync` — this is the one group that does not
        go over the wire.

    .. note::

       The ``received_*`` and ``exif_*`` observations describe one
       specific frame, so they must never be published as fields of
       their own.  SpiriSynq syncs each field independently and promises
       nothing about two of them arriving together or in order, so a
       width published beside an image is a width a peer can read
       against the wrong image.  They are instead written *into* the
       encoded frame by :py:mod:`SpiriCamera.exif` and read back out of
       it, on the authoritative node and on every mirror alike, by
       :py:meth:`Camera._on_image_changed`.  A mirror therefore fills
       these in for itself and stays exact by construction.

       This is why they appear in ``synq_skip_sync`` rather than being
       renamed with a leading underscore, which would also exclude them:
       they are ordinary public attributes that a peer is meant to read.

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
    status : str
        What the camera is doing, in words: :py:data:`STATUS_RUNNING`,
        :py:data:`STATUS_STOPPED`, or the reason it is not running —
        a source string that would not resolve, a device that would not
        open, or a capture that keeps failing.  ``running`` says whether
        frames are flowing; ``status`` says why not when they are not.
    image : bytes
        The most recent encoded frame, in :py:attr:`mimetype` format,
        carrying :py:attr:`exif_tags` in its EXIF.
    exif_enabled : bool
        Whether to tag encoded frames at all.  Turning it off gives byte
        identical frames for an unchanging scene, which psygnal then
        suppresses instead of republishing.
    exif_extra : dict[str, str]
        Tags to merge over the ones the camera derives for itself, for a
        caller or a remote peer with something to add.
    exif_tags : dict[str, str]
        The tags on the current :py:attr:`image`, read back out of it.
    exif_timestamp : float
        Unix timestamp of when the current :py:attr:`image` was captured,
        or ``0.0`` if it carried none.  Set by whichever node took the
        frame, so comparing it against a local clock is only as good as
        the agreement between the two.
    """

    #: Fields excluded from SpiriSynq, on top of the base class's own.
    #:
    #: Every one of these describes the current :py:attr:`image`, and is
    #: derived from its bytes wherever it is needed.  Publishing them
    #: separately would let a peer pair an observation with the wrong
    #: frame; see the class docstring.
    synq_skip_sync = {
        "received_width",
        "received_height",
        "received_ratio",
        "exif_tags",
        "exif_timestamp",
    }

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

    exif_enabled: bool = True
    exif_extra: dict[str, str] = field(default_factory=dict)
    exif_tags: dict[str, str] = field(default_factory=dict)
    exif_timestamp: float = 0.0

    running: bool = False
    status: str = STATUS_STOPPED
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
        # Intent, as opposed to _active's fact. A camera retargeted from a
        # half-typed source string stops running but still wants to, so it
        # can resume by itself once the string resolves and opens.
        self._wants_running = False
        self._background = True
        self._handler: SourceBase | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._cached_frame: np.ndarray | None = None
        self._cached_encoding: tuple[str, int] | None = None
        self._cached_image: bytes = b""
        # Last EXIF failure reported, so a source that cannot be tagged
        # says so once rather than at the full framerate.
        self._exif_complaint: str = ""

        self._resolve(source)

        # React to our own state changing, whoever changed it — a local
        # caller, a UI binding, or a remote peer over SpiriSynq.
        self.events.source_str.connect(self._on_source_str_changed)
        self.events.running.connect(self._on_running_changed)
        # Frames arrive the same way on both sides: we set image, or the
        # network does. Either way the observations come out of its bytes.
        self.events.image.connect(self._on_image_changed)

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
            self.status = "no source configured"
            return

        try:
            handler = resolve_source(source_str)
        except SourceError as exc:
            self._handler = None
            self.source.update(SourceInfo.unresolved(source_str, str(exc)))
            self.status = str(exc)
            logger.warning(f"{self.synq_topic}: cannot resolve {source_str!r}: {exc}")
            return

        self._handler = handler
        self.source.update(handler.describe())
        self.status = STATUS_RUNNING if self._active else STATUS_STOPPED
        logger.debug(f"{self.synq_topic}: resolved {source_str!r} to {handler!r}")

    def _on_source_str_changed(self, source_str: str) -> None:
        """Retarget the camera when the source string changes.

        Resuming is driven by intent rather than by whether the camera
        happens to be running.  A source string typed a character at a
        time goes through states that neither resolve (``/dev/vi``
        resolves but will not open, ``testimag`` does not resolve at
        all) nor run, and the camera must pick itself back up when the
        string finally works instead of waiting to be started by hand.
        """
        wanted = self._wants_running
        self.stop()
        self._resolve(source_str)
        self._wants_running = wanted

        if not wanted or self._handler is None:
            return

        try:
            self.start(background=self._background)
        except CameraError as exc:
            # Still wanted, just not yet: the next edit gets another go.
            self._wants_running = True
            logger.info(f"{self.synq_topic}: not running yet, {exc}")

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
            # Recorded before the attempt, so a camera that was asked to run
            # but could not open yet resumes on the next workable source.
            self._wants_running = True
            self._background = background

            if self._active:
                logger.debug(f"{self.synq_topic}: already running")
                return

            if self._handler is None:
                reason = self.source.error or "no source configured"
                self.status = reason
                raise CameraError(f"{self.synq_topic}: cannot start, {reason}")

            try:
                capabilities = self._handler.open(self.capture_settings())
            except SourceError as exc:
                self._handler.close()
                self.status = str(exc)
                raise CameraError(f"{self.synq_topic}: {exc}") from exc

            self._apply(capabilities)
            self._stop_event.clear()
            self._active = True
            self.running = True
            self.status = STATUS_RUNNING

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
            self._wants_running = False
            if not self._active and self._thread is None and not self._is_source_open():
                return
            self._stop_event.set()
            thread, self._thread = self._thread, None
            self._active = False
            self.running = False
            self.status = STATUS_STOPPED

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

        The encoded frame is tagged, then assigned to
        :py:attr:`~CameraBase.image`, which is what publishes it to
        SpiriSynq.  Assigning is also what fills in
        :py:attr:`~CameraBase.received_width` and the rest, since those
        are read back out of the frame rather than measured here.

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

        encoded = self._tag(self._encode(frame), frame)
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
                if failures:
                    self.status = STATUS_RUNNING
                failures = 0
            except Exception as exc:
                failures += 1
                self.status = f"capture failing: {exc}"
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
    # Frame tagging
    # ------------------------------------------------------------------

    def exif_tags_for_frame(self, frame: np.ndarray) -> dict[str, str]:
        """Build the tags to write into the next encoded frame.

        The override point for tagging.  A subclass with a GPS fix, a
        gimbal angle or a mission identifier to attach should extend what
        this returns; a caller with something simpler to add can put it
        in :py:attr:`~CameraBase.exif_extra`, which is merged over the
        result here and so wins on a clash.

        Dimensions are deliberately absent: the JPEG header already
        states them, and :py:meth:`_on_image_changed` reads them from
        there.  A tag saying something the container also says is a tag
        that can disagree with it.

        Parameters
        ----------
        frame : np.ndarray
            The raw BGR frame about to be encoded.

        Returns
        -------
        dict[str, str]
            Tags to embed.  Empty means embed nothing.
        """
        del frame  # The defaults describe the camera, not the pixels.
        # One clock reading for both tags, so the precise value and the
        # human-readable one can never name different instants.
        captured = time.time()
        tags = {
            exif.TIMESTAMP_TAG: f"{captured:.6f}",
            exif.DATETIME_TAG: time.strftime("%Y:%m:%d %H:%M:%S", time.localtime(captured)),
            "software": f"SpiriCamera {self.synq_topic}",
            "source": self.source.url or self.source_str,
        }
        if self.vendor:
            tags["make"] = self.vendor
        if self.model:
            tags["model"] = self.model
        if self.serial_number:
            tags["serial_number"] = self.serial_number

        tags.update({str(k): str(v) for k, v in self.exif_extra.items()})
        return {name: value for name, value in tags.items() if value}

    def exif_update(self, **tags: str) -> None:
        """Merge tags into :py:attr:`~CameraBase.exif_extra`.

        A convenience for the common case of adding one tag without
        disturbing the others.  Takes effect on the next encoded frame.

        Parameters
        ----------
        **tags : str
            Tags to add or replace.  A value of ``""`` drops the tag.
        """
        merged = dict(self.exif_extra)
        merged.update({name: str(value) for name, value in tags.items()})
        # Reassigned rather than mutated: psygnal watches the attribute,
        # not the dict, so an in-place update would publish nothing.
        self.exif_extra = {name: value for name, value in merged.items() if value}

    def _tag(self, encoded: bytes, frame: np.ndarray) -> bytes:
        """Write this frame's tags into encoded image data.

        Kept apart from :py:meth:`_encode` so that the encode cache still
        earns its keep.  Tags carry a capture timestamp, so every frame's
        final bytes differ even when the pixels do not; splicing an EXIF
        segment onto a cached encode is cheap, re-encoding is not.

        A tagging failure is not worth dropping a frame over, so the
        untagged frame is returned and the reason logged once.
        """
        if not self.exif_enabled:
            return encoded

        try:
            tagged = exif.embed(encoded, self.exif_tags_for_frame(frame))
        except exif.ExifError as error:
            complaint = str(error)
            if complaint != self._exif_complaint:
                self._exif_complaint = complaint
                logger.warning(f"{self.synq_topic}: frame not tagged, {complaint}")
            return encoded

        self._exif_complaint = ""
        return tagged

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_image_changed(self, image: bytes) -> None:
        """Read the observations back out of a newly assigned frame.

        Fires wherever the frame came from: this camera encoding one, or
        SpiriSynq delivering one to a mirror.  That is the whole point of
        keeping these out of the sync set — there is one way to learn
        what a frame is, and it works the same on both sides.
        """
        size = exif.image_size(image) if image else None
        width, height = size or (0, 0)
        self.received_width = width
        self.received_height = height
        self.received_ratio = round(width / height, 4) if height else 0.0

        tags = exif.extract(image) if image else {}
        self.exif_tags = tags
        try:
            self.exif_timestamp = float(tags.get(exif.TIMESTAMP_TAG, 0.0))
        except ValueError:
            self.exif_timestamp = 0.0

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
