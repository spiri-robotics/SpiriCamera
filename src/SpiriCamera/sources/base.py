"""Source domain primitives: URLs, capabilities, and the handler contract.

This module owns everything about *where frames come from*.  It knows
nothing about :py:class:`~SpiriCamera.camera.Camera`: handlers resolve
their own source strings, open their own devices, and report what they
can do.  The camera layer asks for raw frames and does the encoding and
publishing on its own.

The dependency direction is strictly one-way::

    camera.py  ->  sources/  ->  sources/base.py

so a source handler must never import the camera module.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass, fields, replace
from typing import ClassVar, Self

import numpy as np
from SpiriSynq.syncable_objects import SubSyncableDataclass

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourceError(Exception):
    """Base class for source resolution, open, and read failures."""


class UnknownSourceError(SourceError):
    """Raised when no registered handler recognises a source string."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceURL:
    """A source string split into its scheme and target.

    Attributes
    ----------
    raw : str
        The original (stripped) source string.
    scheme : str
        Lowercased scheme, or ``""`` for schemeless strings such as
        ``/dev/video0`` or a bare camera index.
    target : str
        Everything after ``://``, or the whole string when schemeless.
    """

    raw: str
    scheme: str
    target: str

    @classmethod
    def parse(cls, source_str: str) -> SourceURL:
        """Split a source string into scheme and target.

        Parameters
        ----------
        source_str : str
            The source string to parse.

        Returns
        -------
        SourceURL
            The split representation.

        Raises
        ------
        SourceError
            If the source string is empty or whitespace only.
        """
        if not source_str or not source_str.strip():
            raise SourceError("Source string cannot be empty")

        raw = source_str.strip()
        scheme, separator, target = raw.partition("://")
        if not separator:
            return cls(raw=raw, scheme="", target=raw)
        return cls(raw=raw, scheme=scheme.lower(), target=target)

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class CaptureSettings:
    """What the camera is asking the source to produce.

    These are upper bounds, not exact dimensions: a source is free to
    deliver something smaller, and reports what it settled on through
    :py:class:`SourceCapabilities`.

    .. note::

       Do not shorten these to ``width`` / ``height`` / ``framerate``.
       The ``max_`` prefix is the contract: a source may answer a
       request for 1920x1080 with a 640x480 frame and still be correct.
       A bare ``width`` would read as "the width you will get", which is
       a promise no source makes.  The names also match the
       :py:class:`~SpiriCamera.camera.CameraBase` fields they are
       copied from, so the two stay greppable together.

    Attributes
    ----------
    max_width : int
        Largest acceptable frame width in pixels.
    max_height : int
        Largest acceptable frame height in pixels.
    max_framerate : int
        Highest requested frames per second.
    """

    max_width: int = 1920
    max_height: int = 1080
    max_framerate: int = 30


@dataclass(frozen=True)
class Intrinsics:
    """A pinhole camera model, at the resolution it was measured at.

    The model is in pixels of a ``width`` x ``height`` image.  A frame of
    another size with the same aspect -- a camera asked for a smaller
    ``max_width``, say -- uses :py:meth:`scaled` to that size, rather than
    the numbers as they are.

    Synced as one value, not field by field, so a peer never sees one
    report's focal length with another's size.

    Attributes
    ----------
    width : int
        Image width the model is in, in pixels.
    height : int
        Image height the model is in, in pixels.
    fx : float
        Horizontal focal length, in pixels.
    fy : float
        Vertical focal length, in pixels.
    cx : float
        Principal point, horizontally, in pixels from the left edge.
    cy : float
        Principal point, vertically, in pixels from the top edge.
    origin : str
        Where it came from: ``manual`` (the camera's settings), ``source``
        (the source reported it, e.g. a Gazebo sensor's ``camera_info``)
        or ``mavlink`` (the camera's MAVLink component or autopilot,
        through SpiriMav).  See :py:data:`~SpiriCamera.camera.INTRINSICS_ORIGINS`.
    """

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    origin: str = ""

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"image size {self.width}x{self.height} is not positive")
        if not (self.fx > 0 and self.fy > 0):
            raise ValueError(f"focal lengths {self.fx}, {self.fy} are not positive")

    @classmethod
    def from_fov(
        cls,
        width: int,
        height: int,
        hfov: float,
        vfov: float = 0.0,
        origin: str = "",
    ) -> Intrinsics:
        """The model of a centred lens with a given field of view.

        Parameters
        ----------
        width, height : int
            Image size to express the model in.
        hfov : float
            Horizontal field of view, in degrees.
        vfov : float
            Vertical field of view, in degrees.  ``0`` assumes square
            pixels, so it follows from ``hfov`` and the image's aspect.
        origin : str
            See the class attributes.
        """
        if not 0 < hfov < 180 or not 0 <= vfov < 180:
            raise ValueError(f"field of view {hfov}°x{vfov}° is not in (0°, 180°)")
        fx = width / 2 / math.tan(math.radians(hfov) / 2)
        fy = height / 2 / math.tan(math.radians(vfov) / 2) if vfov else fx
        return cls(width, height, fx, fy, width / 2, height / 2, origin)

    @property
    def hfov(self) -> float:
        """Horizontal field of view through the principal point, in degrees."""
        return math.degrees(
            math.atan(self.cx / self.fx) + math.atan((self.width - self.cx) / self.fx)
        )

    @property
    def vfov(self) -> float:
        """Vertical field of view through the principal point, in degrees."""
        return math.degrees(
            math.atan(self.cy / self.fy) + math.atan((self.height - self.cy) / self.fy)
        )

    def scaled(self, width: int, height: int) -> Intrinsics:
        """The same lens, for an image resized to ``width`` x ``height``."""
        sx, sy = width / self.width, height / self.height
        return replace(
            self,
            width=width,
            height=height,
            fx=self.fx * sx,
            fy=self.fy * sy,
            cx=self.cx * sx,
            cy=self.cy * sy,
        )

    def __str__(self) -> str:
        return f"{self.hfov:.1f}°x{self.vfov:.1f}° at {self.width}x{self.height}"


@dataclass(frozen=True)
class SourceCapabilities:
    """What a source reports it can provide, once opened.

    .. note::

       These keep the ``max_supported_`` prefix for the same reason
       :py:class:`CaptureSettings` keeps ``max_``: they are ceilings the
       device advertises, not a guarantee about any particular frame,
       and they are copied verbatim onto the matching
       :py:class:`~SpiriCamera.camera.CameraBase` fields.

    Attributes
    ----------
    max_supported_width : int | None
        Largest width the source can deliver, or ``None`` if it renders
        at whatever size it is asked for.
    max_supported_height : int | None
        Largest height the source can deliver, or ``None`` if it is
        resolution independent.
    max_supported_framerate : int | None
        Highest framerate the source can deliver, or ``None`` if
        unconstrained.
    vendor : str
        Device vendor, when the source can determine one.
    model : str
        Device model, when the source can determine one.
    serial_number : str
        Device serial number, when the source can determine one.
    intrinsics : Intrinsics | None
        The lens, when the source knows it -- a simulated camera does,
        exactly; most devices don't say.  Its ``origin`` is filled in
        by the camera.
    frame : str
        The SpiriSynq topic of the
        :py:class:`~SpiriSynq.example_types.position.Position` the frames
        are seen from, when they are some other object's view: a relayed
        camera's.  The camera follows its pose unless it has been placed
        itself.  Empty for a source that is the camera.
    """

    max_supported_width: int | None = None
    max_supported_height: int | None = None
    max_supported_framerate: int | None = None
    vendor: str = ""
    model: str = ""
    serial_number: str = ""
    intrinsics: Intrinsics | None = None
    frame: str = ""


@dataclass
class SourceInfo(SubSyncableDataclass):
    """Network-transparent description of a camera's resolved source.

    This is the source-shaped half of the camera's synced state: it
    travels over SpiriSynq so a remote peer can see what a camera is
    pointed at without having to parse the source string itself.

    Attributes
    ----------
    url : str
        The source string this description was resolved from.
    scheme : str
        Normalised scheme (``v4l``, ``rtsp``, ``testimage``, ...).
    target : str
        Scheme-specific target: device path, stream URL, or image name.
    handler : str
        Name of the handler class that claimed the source.
    error : str
        Empty when the source resolved cleanly, otherwise the reason it
        could not be resolved.
    """

    url: str = ""
    scheme: str = ""
    target: str = ""
    handler: str = ""
    error: str = ""

    @classmethod
    def unresolved(cls, url: str, error: str) -> SourceInfo:
        """Build a description for a source string that could not be resolved.

        Parameters
        ----------
        url : str
            The offending source string.
        error : str
            Human-readable reason the source was rejected.

        Returns
        -------
        SourceInfo
            A description carrying only ``url`` and ``error``.
        """
        return cls(url=url, error=error)

    def update(self, other: SourceInfo) -> None:
        """Copy another description's fields into this one, field by field.

        Updating in place keeps the object identity stable, so UI
        bindings and sync subscriptions attached to this instance
        survive a source change, and only the fields that actually
        changed are published.

        Parameters
        ----------
        other : SourceInfo
            The description to copy from.
        """
        for entry in fields(self):
            setattr(self, entry.name, getattr(other, entry.name))


# ---------------------------------------------------------------------------
# Handler contract
# ---------------------------------------------------------------------------


class SourceBase(abc.ABC):
    """Abstract base for camera source handlers.

    A handler is responsible for its own protocol end to end: deciding
    whether it recognises a source string, parsing it, opening the
    underlying device, and producing raw BGR frames.  Registration is
    implicit — :py:mod:`SpiriCamera.sources` discovers every concrete
    subclass that has been imported.

    Subclasses declare the schemes they own via :py:attr:`schemes` and
    implement :py:meth:`open`, :py:meth:`close`, :py:meth:`read`, and
    :py:attr:`is_open`.  A handler that accepts schemeless strings (such
    as a V4L2 device path) also overrides :py:meth:`handles`.

    Attributes
    ----------
    schemes : ClassVar[tuple[str, ...]]
        URL schemes this handler claims, without the ``://`` separator.
    """

    schemes: ClassVar[tuple[str, ...]] = ()

    def __init__(self, url: SourceURL) -> None:
        """Create a handler for an already-parsed source URL.

        Parameters
        ----------
        url : SourceURL
            The parsed source string, as accepted by :py:meth:`handles`.
        """
        self.url = url

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    @classmethod
    def handles(cls, url: SourceURL) -> bool:
        """Report whether this handler recognises a source URL.

        The default implementation matches :py:attr:`schemes`.  Override
        to accept schemeless forms.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.

        Returns
        -------
        bool
            True if this handler can serve the URL.
        """
        return url.scheme in cls.schemes

    @classmethod
    def from_url(cls, url: SourceURL) -> Self:
        """Instantiate this handler for a source URL it has claimed.

        Override when a handler needs to validate or normalise the
        target before construction.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.

        Returns
        -------
        Self
            A handler instance, not yet opened.

        Raises
        ------
        SourceError
            If the target is malformed for this scheme.
        """
        return cls(url)

    # ------------------------------------------------------------------
    # Description
    # ------------------------------------------------------------------

    @property
    def scheme(self) -> str:
        """Normalised scheme name, filled in for schemeless sources."""
        return self.url.scheme or (self.schemes[0] if self.schemes else "")

    @property
    def target(self) -> str:
        """Scheme-specific target: device path, stream URL, or image name."""
        return self.url.target

    def describe(self) -> SourceInfo:
        """Summarise this source for the camera's synced state.

        Returns
        -------
        SourceInfo
            A network-transparent description of this handler.
        """
        return SourceInfo(
            url=self.url.raw,
            scheme=self.scheme,
            target=self.target,
            handler=type(self).__name__,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def is_open(self) -> bool:
        """Whether the underlying device is currently open."""

    @abc.abstractmethod
    def open(self, settings: CaptureSettings) -> SourceCapabilities:
        """Open the underlying device and negotiate capture settings.

        Calling :py:meth:`open` on an already-open source is a no-op that
        re-reports the current capabilities.

        Parameters
        ----------
        settings : CaptureSettings
            The resolution and framerate the camera is asking for.

        Returns
        -------
        SourceCapabilities
            What the source will actually deliver.

        Raises
        ------
        SourceError
            If the device cannot be opened.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release the underlying device.

        Closing an already-closed source is a no-op.
        """

    @abc.abstractmethod
    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        """Produce a single frame.

        Handlers return raw pixels; JPEG encoding and publication belong
        to the camera layer.

        Parameters
        ----------
        settings : CaptureSettings
            The resolution and framerate currently requested.  Sources
            that render on demand should honour this on every call so
            that live resolution changes take effect.

        Returns
        -------
        np.ndarray | None
            A BGR frame, or ``None`` when no new frame is available.
            Returning the *same* array object on consecutive calls tells
            the camera the frame is unchanged and lets it reuse its
            previous encode.

        Raises
        ------
        SourceError
            If the source has failed in a way a retry will not fix.
        """

    def frame_tags(self) -> dict[str, str]:
        """Tags that arrived with the frame :py:meth:`read` last returned.

        For sources whose frames were already tagged before they got
        here -- another camera's output, say -- so the metadata survives
        being decoded and re-encoded.  The camera merges these under its
        own tags; see
        :py:meth:`~SpiriCamera.camera.Camera.exif_tags_for_frame`.

        Returns
        -------
        dict[str, str]
            The inherited tags.  The default reports none.
        """
        return {}

    def frame_intrinsics(self) -> Intrinsics | None:
        """The lens of the frame :py:meth:`read` last returned, if it changes.

        For sources whose lens can change while open -- another camera's
        output, whose lens zooms -- so the camera follows it rather than
        keeping the one :py:meth:`open` reported.

        Returns
        -------
        Intrinsics | None
            The lens now, or ``None`` to keep the last one reported.  The
            default keeps what :py:meth:`open` said.
        """
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.url.raw!r})"
