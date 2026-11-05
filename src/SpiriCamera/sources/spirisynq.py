"""SpiriSynq camera source: another camera's published frames as input.

A ``spirisynq://<topic>`` source string names a camera already running
somewhere on the SpiriSynq network, by the same absolute topic
``Camera.from_topic`` takes (``some-host/spiricamera_dev_video0``).
Instead of *mirroring* that camera -- a read-only view of its fields --
a camera built on this source *consumes* it: every upstream frame is
decoded, fitted to this camera's own ``max_width``/``max_height``, and
handed back as a raw frame like any other source's.  Everything a
camera does with a raw frame then applies to it on its own terms: its
own overlays, its own quality, its own framerate, its own topic.

The upstream is reached without importing :py:mod:`SpiriCamera.camera`
(see :py:mod:`SpiriCamera.sources.base` for why that direction is
forbidden): this handler only needs an object with an ``image`` field
and an ``events.image`` signal, which any SpiriSynq mirror of a camera
provides, whatever class it was built as.

The frames are the upstream's view, so the upstream's lens and pose come
with them: the lens through :py:meth:`SpiriSynqSource.frame_intrinsics`,
live, and the pose as the capabilities' ``frame``, the upstream's topic,
which the consuming camera follows unless it has been placed itself.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping

import cv2
import numpy as np
from loguru import logger
from SpiriSynq.session import Session, current_session

from SpiriCamera import exif
from SpiriCamera.sources.base import (
    CaptureSettings,
    Intrinsics,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceURL,
)


def fit_within(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    """Shrink a frame size to fit a bounding box, keeping its aspect ratio.

    Never enlarges: an upstream frame that already fits is left at its
    own size, since upscaling only spends bandwidth on pixels that carry
    no more detail than the originals.

    Parameters
    ----------
    width, height : int
        The frame's own size.
    max_width, max_height : int
        The bounding box.

    Returns
    -------
    tuple[int, int]
        The fitted size, each at least 1.
    """
    scale = min(1.0, max_width / width, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def shrink(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Resize ``frame`` down to ``size`` (``(width, height)``).

    Bilinear down to half size, area-averaged below that: bilinear looks
    the same for a small reduction at a fifth of the cost (2.3 ms against
    12.3 ms for 720p to 480p on a Raspberry Pi 4), but samples too few
    pixels to keep a larger one from aliasing.
    """
    scale = size[1] / frame.shape[0]
    interpolation = cv2.INTER_LINEAR if scale >= 0.5 else cv2.INTER_AREA
    return cv2.resize(frame, size, interpolation=interpolation)


def find_published(session: Session, topic: str) -> dict | None:
    """Look up the metadata of the object published at exactly ``topic``.

    Uses :py:meth:`~SpiriSynq.session.Session.list_topics` rather than an
    RPC, because an absent topic is simply no reply there, where an RPC
    to it logs an error -- and a camera waiting for its upstream asks
    every second.

    Parameters
    ----------
    session : Session
        The session to ask on.
    topic : str
        Absolute topic of the object.

    Returns
    -------
    dict | None
        Its ``sr_metadata`` (``topic``, ``classes``,
        ``authoritive_node``), or ``None`` if nothing answers for it.
    """
    for metadata in session.list_topics(prefix=topic):
        if metadata.get("topic") == topic:
            return metadata
    return None


def mirror_topic(session: Session, topic: str) -> tuple[object, dict]:
    """Mirror the SpiriSynq object at ``topic``, preferring its real class.

    Uses the class this session already has registered for the
    object's type when there is one -- in a process that is running a
    :py:class:`~SpiriCamera.camera.Camera` of its own, that is always
    true of a camera upstream -- and only falls back to
    :py:meth:`~SpiriSynq.session.Session.from_topic_untyped` otherwise.

    .. note::

       The order matters.  ``from_topic_untyped`` registers its
       synthesized class for the object's type tag unconditionally,
       replacing a real class already registered under that tag, so
       calling it on a camera's topic breaks every later
       ``Camera.from_topic`` on the same session.

    Parameters
    ----------
    session : Session
        The session to mirror on.
    topic : str
        Absolute topic of the object.

    Returns
    -------
    tuple[object, dict]
        A live mirror, kept up to date by SpiriSynq, and the metadata
        it was found by (see :py:func:`find_published`).

    Raises
    ------
    SourceError
        If nothing is published at ``topic``.
    """
    metadata = find_published(session, topic)
    if metadata is None:
        raise SourceError(f"nothing is published at {topic!r}")
    tags = metadata.get("classes") or []
    registered = session.type_registry.constructor.yaml_constructors
    if tags and tags[0] in registered:
        return session.from_topic(topic), metadata
    return session.from_topic_untyped(topic), metadata


class SpiriSynqSource(SourceBase):
    """Reads frames from another camera published over SpiriSynq.

    ``open()`` mirrors the upstream camera on the current default
    session (:py:meth:`Camera.start <SpiriCamera.camera.Camera.start>`
    makes that the camera's own) and subscribes to its ``image``.  New
    frames are only stored as they arrive; decoding and resizing happen
    in :py:meth:`read`, at this camera's own framerate, so an upstream
    running faster than this camera costs one decode per frame actually
    used rather than one per frame sent.

    Each upstream frame's EXIF tags come along with it through
    :py:meth:`frame_tags`, so GPS fixes and the original capture time
    survive the re-encode.

    Losing the upstream closes this source, which the camera answers by
    waiting for the topic and mirroring it afresh once it is back.  An
    upstream that shuts down cleanly says so (a SpiriSynq tombstone); one
    that crashed or dropped off the network cannot, so once no frame has
    arrived for :py:data:`STALE_AFTER` seconds, :py:meth:`read` asks the
    network whether it is still there -- and whether it is still the
    same process, since a restarted one deserves a fresh mirror.  An
    upstream that is merely stopped still answers, and is kept.
    """

    schemes = ("spirisynq",)

    #: Seconds without a new upstream frame before checking the upstream
    #: is still there, and the least time between two such checks.
    STALE_AFTER = 2.0

    def __init__(self, url: SourceURL) -> None:
        super().__init__(url)
        self._upstream: object | None = None
        self._session: Session | None = None
        self._node: str = ""
        self._topic: str = ""
        self._gone = threading.Event()
        self._last_arrival = 0.0
        self._last_probe = 0.0
        self._lock = threading.Lock()
        self._latest_image: bytes = b""
        self._frame: np.ndarray | None = None
        self._frame_bounds: tuple[int, int] | None = None
        self._frame_image: bytes = b""
        self._tags: dict[str, str] = {}

    @classmethod
    def from_url(cls, url: SourceURL) -> SpiriSynqSource:
        if not url.target.strip("/"):
            raise SourceError(f"No topic given in {url.raw!r}; expected spirisynq://<host>/<topic>")
        return cls(url)

    @property
    def target(self) -> str:
        """The upstream camera's SpiriSynq topic, as given.

        Absolute, or relative to this session's base topic (``./camera1``,
        this host's camera1); :py:meth:`open` resolves it.
        """
        return self.url.target.strip("/")

    @property
    def is_open(self) -> bool:
        return self._upstream is not None

    def open(self, settings: CaptureSettings) -> SourceCapabilities:
        del settings  # Applied per frame in read(), so live changes work.
        if self._upstream is not None:
            return self._capabilities(self._upstream)

        session = current_session.get()
        try:
            self._topic = session.resolve_topic(self.target)
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
        try:
            upstream, metadata = mirror_topic(session, self._topic)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"could not reach SpiriSynq topic {self._topic!r}: {exc}") from exc

        events = getattr(upstream, "events", None)
        if not hasattr(upstream, "image") or not hasattr(events, "image"):
            upstream.close()  # type: ignore[attr-defined]
            raise SourceError(f"{self._topic!r} is not a camera: it has no image")

        now = time.monotonic()
        with self._lock:
            self._latest_image = upstream.image or b""  # type: ignore[attr-defined]
            self._last_arrival = now
        self._last_probe = now
        self._gone.clear()
        self._session = session
        self._node = str(metadata.get("authoritive_node", ""))
        events.image.connect(self._on_upstream_image)
        tombstone = getattr(upstream, "synq_signal_tombstone", None)
        if tombstone is not None:
            tombstone.connect(self._gone.set)
        self._upstream = upstream
        logger.info(f"{self!r} consuming {self._topic}")
        return self._capabilities(upstream)

    def _on_upstream_image(self, image: bytes) -> None:
        """Called on SpiriSynq's thread: just keep the bytes for :py:meth:`read`."""
        with self._lock:
            self._latest_image = image or b""
            self._last_arrival = time.monotonic()

    def close(self) -> None:
        upstream, self._upstream = self._upstream, None
        if upstream is not None:
            upstream.events.image.disconnect(self._on_upstream_image)  # type: ignore[attr-defined]
            tombstone = getattr(upstream, "synq_signal_tombstone", None)
            if tombstone is not None:
                tombstone.disconnect(self._gone.set)
            upstream.close()  # type: ignore[attr-defined]
        self._session = None
        with self._lock:
            self._latest_image = b""
        self._frame = None
        self._frame_bounds = None
        self._frame_image = b""
        self._tags = {}

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        """Return the latest upstream frame, fitted inside the requested bounds.

        The same array object comes back until either the upstream sends
        a new frame or the bounds change, which lets the camera reuse its
        previous encode.

        Returns
        -------
        np.ndarray | None
            A BGR frame, or ``None`` until the upstream has sent one.

        Raises
        ------
        SourceError
            If the source is not open, or the upstream has gone away, in
            which case the source has closed itself.
        """
        if self._upstream is None:
            raise SourceError(f"{self!r} is not open; call open() before read()")
        self._check_upstream()

        with self._lock:
            image = self._latest_image
        if not image:
            return None

        bounds = (settings.max_width, settings.max_height)
        if image is self._frame_image and bounds == self._frame_bounds:
            return self._frame

        decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            logger.debug(f"{self!r}: upstream frame did not decode, skipping it")
            return self._frame

        height, width = decoded.shape[:2]
        size = fit_within(width, height, settings.max_width, settings.max_height)
        if size != (width, height):
            decoded = shrink(decoded, size)

        self._frame = decoded
        self._frame_bounds = bounds
        self._frame_image = image
        self._tags = exif.extract(image)
        return decoded

    def _check_upstream(self) -> None:
        """Close and raise if the upstream is gone; see the class docstring."""
        if self._gone.is_set():
            self._lose("closed its topic")

        now = time.monotonic()
        with self._lock:
            quiet_for = now - self._last_arrival
        if quiet_for < self.STALE_AFTER or now - self._last_probe < self.STALE_AFTER:
            return
        self._last_probe = now

        assert self._session is not None
        metadata = find_published(self._session, self._topic)
        if metadata is None:
            self._lose("stopped answering")
        elif str(metadata.get("authoritive_node", "")) != self._node:
            self._lose("was restarted")

    def _lose(self, reason: str) -> None:
        """Give up on the current upstream mirror."""
        self.close()
        raise SourceError(f"upstream {self._topic!r} {reason}")

    def frame_tags(self) -> dict[str, str]:
        """The EXIF tags the upstream wrote into the frame last read."""
        return self._tags

    def frame_intrinsics(self) -> Intrinsics | None:
        """The upstream's lens now: the frames are its view, resized."""
        return _lens(getattr(self._upstream, "intrinsics", None))

    def _capabilities(self, upstream: object) -> SourceCapabilities:
        """Pass the upstream's own reported limits, identity, lens and pose through."""
        return SourceCapabilities(
            intrinsics=_lens(getattr(upstream, "intrinsics", None)),
            frame=self._topic,
            max_supported_width=getattr(upstream, "max_supported_width", None),
            max_supported_height=getattr(upstream, "max_supported_height", None),
            max_supported_framerate=getattr(upstream, "max_supported_framerate", None),
            vendor=getattr(upstream, "vendor", "") or "",
            model=getattr(upstream, "model", "") or "",
            serial_number=getattr(upstream, "serial_number", "") or "",
        )


def _lens(value: object) -> Intrinsics | None:
    """An upstream's ``intrinsics``, typed or as the plain mapping a mirror may hold."""
    if isinstance(value, Intrinsics) or value is None:
        return value
    if isinstance(value, Mapping):
        try:
            return Intrinsics(**value)
        except (TypeError, ValueError):
            return None
    return None
