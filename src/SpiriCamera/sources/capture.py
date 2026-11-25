"""Shared ``cv2.VideoCapture`` plumbing for device-backed sources.

The local V4L2 handler, the network stream handler, and the file
handler are thin wrappers around OpenCV's capture object.  Everything
they have in common — opening, applying requested settings, reading
back what the device negotiated, and grabbing frames — lives here.

A live source (see :py:attr:`SourceBase.live`) is read on a thread of
its own, as fast as the device delivers, and :py:meth:`OpenCVSource.read`
hands back the newest frame.  Without it, a camera that reads a little
slower than the device sends leaves frames queueing in front of it: in
FFmpeg's and the kernel's buffers for a network stream, where seconds of
video can wait before overflowing, and in the driver's for a V4L2 device.
"""

from __future__ import annotations

import abc
import os
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np
from loguru import logger

from SpiriCamera.sources.base import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
)

#: How long :py:meth:`OpenCVSource.read` on a live source waits for a new
#: frame before returning ``None``, so a camera that is stopping isn't
#: kept waiting on a stream that has gone quiet.
_LIVE_READ_WAIT = 0.5

#: How early, as a fraction of the frame interval, a live source's frame
#: may arrive and still count, so a frame a little early on a device
#: running at exactly ``max_framerate`` isn't dropped for its jitter.
_FRAMERATE_SLACK = 0.25


#: Where OpenCV takes FFmpeg's options from, as ``key;value|key;value``.
#: It is the only way in, and is read at every open.
FFMPEG_OPTIONS_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"

#: Serialises opens that set :py:data:`FFMPEG_OPTIONS_ENV`, so one
#: source's options never reach another's capture.
_ENV_LOCK = threading.Lock()


def merge_ffmpeg_options(defaults: dict[str, str], configured: str) -> str:
    """``defaults`` under the options a deployment set, in OpenCV's format.

    Parameters
    ----------
    defaults : dict[str, str]
        A source's own options.
    configured : str
        :py:data:`FFMPEG_OPTIONS_ENV` as the deployment set it, which
        wins key by key.

    Returns
    -------
    str
        ``key;value`` pairs joined by ``|``.
    """
    options = dict(defaults)
    extra: list[str] = []
    for entry in filter(None, configured.split("|")):
        key, sep, value = entry.partition(";")
        if sep:
            options[key] = value
        else:
            extra.append(entry)
    return "|".join([f"{key};{value}" for key, value in options.items()] + extra)


@dataclass(frozen=True)
class _Grab:
    """One read of the device, by the grabber thread."""

    #: Counts grabs, so a reader can tell a new one from one it has seen.
    seq: int
    #: The frame, or ``None`` for a failed read.
    frame: np.ndarray | None
    #: When the read returned.
    arrived: float
    #: How long the read blocked for.
    took: float


class OpenCVSource(SourceBase):
    """Base for sources backed by ``cv2.VideoCapture``.

    Subclasses supply the capture target via :py:meth:`capture_target`
    and may extend :py:meth:`device_metadata` to report vendor, model,
    and serial number.
    """

    def __init__(self, url) -> None:
        super().__init__(url)
        self._capture: cv2.VideoCapture | None = None
        #: Settings last pushed onto the device, so :py:meth:`read` can
        #: tell a live change from a repeat of the same request.
        self._applied: CaptureSettings | None = None
        #: What :py:meth:`open` negotiated, so a live source's capture
        #: isn't queried while its grabber is reading it.
        self._capabilities: SourceCapabilities | None = None

        # A live source's grabber, and what it shares with read(), all
        # under _grabbed.  Only the grabber touches the capture once it
        # runs: read() leaves settings in _wanted for it to apply.
        self._grabber: threading.Thread | None = None
        self._stopping = threading.Event()
        self._grabbed = threading.Condition()
        self._wanted: CaptureSettings | None = None
        #: The grabber's newest read, and its newest that got a frame.
        self._latest: _Grab | None = None
        self._latest_frame: _Grab | None = None
        #: The newest grab read() has handed back or passed over.
        self._seen_seq = 0
        #: When the next frame is due, at ``max_framerate``.
        self._next_due = 0.0
        #: The grab read() last handed back, so a subclass can see how
        #: long the device took to produce it.
        self._last_grab: _Grab | None = None

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

    def capture_params(self) -> list[int]:
        """Return extra ``cv2.VideoCapture`` open parameters.

        The default implementation passes none.  Subclasses override to
        set properties that only take effect when given at open time,
        such as FFmpeg's open and read timeouts.

        Returns
        -------
        list[int]
            Alternating ``cv2.CAP_PROP_*`` ids and values.
        """
        return []

    def ffmpeg_options(self) -> dict[str, str]:
        """Return FFmpeg options this source opens with by default.

        Any the deployment sets in :py:data:`FFMPEG_OPTIONS_ENV` win,
        key by key, and the variable is put back once the source opens.
        The default implementation adds none.

        Returns
        -------
        dict[str, str]
            FFmpeg option names and values.
        """
        return {}

    def _open_capture(self, target: str | int, params: list[int]) -> cv2.VideoCapture:
        """``cv2.VideoCapture`` on ``target``, with this source's FFmpeg options."""

        def open_it() -> cv2.VideoCapture:
            if params:
                return cv2.VideoCapture(target, cv2.CAP_ANY, params)
            return cv2.VideoCapture(target)

        defaults = self.ffmpeg_options()
        if not defaults:
            return open_it()
        with _ENV_LOCK:
            configured = os.environ.get(FFMPEG_OPTIONS_ENV)
            os.environ[FFMPEG_OPTIONS_ENV] = merge_ffmpeg_options(defaults, configured or "")
            try:
                return open_it()
            finally:
                if configured is None:
                    del os.environ[FFMPEG_OPTIONS_ENV]
                else:
                    os.environ[FFMPEG_OPTIONS_ENV] = configured

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
            if self._grabber is not None and self._capabilities is not None:
                return self._capabilities
            return self._negotiated_capabilities()

        target = self.capture_target()
        logger.debug(f"{self!r} opening capture target {target!r}")

        capture = self._open_capture(target, self.capture_params())
        if not capture.isOpened():
            capture.release()
            raise SourceError(f"Failed to open capture source: {self.url.raw!r}")

        self._capture = capture
        self._apply(settings)
        self._applied = settings

        capabilities = self._negotiated_capabilities()
        self._capabilities = capabilities
        logger.info(
            f"{self!r} opened | "
            f"{capabilities.max_supported_width}x{capabilities.max_supported_height} "
            f"@ {capabilities.max_supported_framerate}fps"
        )
        if self.live:
            self._start_grabber(capture)
        return capabilities

    def close(self) -> None:
        """Release the capture device.

        A live source's grabber is stopped first, which can take as long
        as the read it is blocked in: the capture can't be released
        under it.
        """
        self._stop_grabber()
        if self._capture is not None:
            self._capture.release()
            self._capture = None
            self._applied = None
            self._capabilities = None
            logger.debug(f"{self!r} closed")

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        """Grab a single BGR frame from the device.

        Re-applies ``settings`` first if they differ from what is
        currently on the device, so a live change to ``max_width`` and
        friends takes effect on the next frame rather than only at the
        next ``open()``.  Most devices accept a resolution change
        mid-stream; one that does not simply keeps delivering its
        current size, which :py:attr:`CameraBase.received_width` and
        friends will still report correctly.

        A live source hands back the newest frame its grabber has read
        that this method hasn't, waiting up to :py:data:`_LIVE_READ_WAIT`
        for one, and passes over frames that arrive sooner than
        ``max_framerate`` allows.  Frames in between are dropped: only
        the newest is worth showing.

        Parameters
        ----------
        settings : CaptureSettings
            Requested capture settings.

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

        if self._grabber is not None:
            return self._read_live(settings)

        if settings != self._applied:
            logger.debug(f"{self!r} re-applying {settings}")
            self._apply(settings)
            self._applied = settings

        ok, frame = self._capture.read()
        if not ok or frame is None:
            return None
        return frame

    # ------------------------------------------------------------------
    # Live sources
    # ------------------------------------------------------------------

    def _start_grabber(self, capture: cv2.VideoCapture) -> None:
        """Start reading ``capture`` on a thread of its own."""
        with self._grabbed:
            self._latest = None
            self._latest_frame = None
            self._seen_seq = 0
            self._next_due = 0.0
            self._wanted = None
        self._last_grab = None
        # An event per grabber, so one being stopped can't be restarted
        # by a reopen that races it.
        self._stopping = threading.Event()
        self._grabber = threading.Thread(
            target=self._grab_loop,
            args=(capture, self._stopping),
            name=f"grab {self.url.raw}",
            daemon=True,
        )
        self._grabber.start()

    def _stop_grabber(self) -> None:
        """Stop the grabber and wait for its read to return."""
        grabber, self._grabber = self._grabber, None
        if grabber is None:
            return
        self._stopping.set()
        with self._grabbed:
            self._grabbed.notify_all()
        grabber.join()

    def _grab_loop(self, capture: cv2.VideoCapture, stopping: threading.Event) -> None:
        """Read ``capture`` as fast as it delivers, keeping only the newest frame."""
        seq = 0
        while not stopping.is_set():
            with self._grabbed:
                wanted = self._wanted
            if wanted is not None and wanted != self._applied:
                logger.debug(f"{self!r} re-applying {wanted}")
                self._apply(wanted, capture)
                with self._grabbed:
                    self._applied = wanted
                    self._grabbed.notify_all()

            started = time.monotonic()
            try:
                ok, frame = capture.read()
            except cv2.error as exc:
                logger.debug(f"{self!r} read failed: {exc}")
                ok, frame = False, None
            arrived = time.monotonic()
            seq += 1
            grab = _Grab(seq, frame if ok else None, arrived, arrived - started)
            with self._grabbed:
                self._latest = grab
                if grab.frame is not None:
                    self._latest_frame = grab
                self._grabbed.notify_all()
            if grab.frame is None:
                # A device that has gone away fails every read at once;
                # don't spin on it while read() notices.
                stopping.wait(0.01)
        logger.debug(f"{self!r} grabber stopped")

    def _read_live(self, settings: CaptureSettings) -> np.ndarray | None:
        """The newest frame the grabber has read, at most ``max_framerate``."""
        interval = 1.0 / settings.max_framerate if settings.max_framerate > 0 else 0.0
        slack = interval * _FRAMERATE_SLACK
        deadline = time.monotonic() + _LIVE_READ_WAIT
        with self._grabbed:
            if settings != self._applied:
                self._wanted = settings
            while self._grabber is not None:
                grab = self._unseen()
                # Settings asked for are on the device before read() returns.
                if grab is not None and self._applied == settings:
                    self._seen_seq = max(self._seen_seq, grab.seq)
                    if grab.frame is None:
                        return None
                    if grab.arrived >= self._next_due - slack:
                        self._next_due = max(
                            self._next_due + interval, grab.arrived + interval - slack
                        )
                        self._last_grab = grab
                        return grab.frame
                    # Too soon for max_framerate: wait for the next one.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._grabbed.wait(remaining)
        return None

    def _unseen(self) -> _Grab | None:
        """The grab read() should look at next, if there is one; holds ``_grabbed``.

        The newest frame not yet seen, or else the newest failed read,
        which read() reports as a dropped frame.
        """
        latest, frame = self._latest, self._latest_frame
        if frame is not None and frame.seq > self._seen_seq:
            return frame
        if latest is not None and latest.seq > self._seen_seq:
            return latest
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply(self, settings: CaptureSettings, capture: cv2.VideoCapture | None = None) -> None:
        """Push requested settings onto the device, which may clamp them."""
        capture = capture if capture is not None else self._capture
        assert capture is not None
        if settings.max_width > 0:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, settings.max_width)
        if settings.max_height > 0:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.max_height)
        if settings.max_framerate > 0:
            capture.set(cv2.CAP_PROP_FPS, settings.max_framerate)

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
