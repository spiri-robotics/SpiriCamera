"""Base class for camera source handlers."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class SourceRegistrationError(Exception):
    """Raised when a source cannot be registered or parsed."""

    message: str = "Source registration error"

    def __str__(self) -> str:
        return self.message


class SourceBase(abc.ABC):
    """Abstract base for camera source handlers.

    Subclasses define their supported URL prefixes via the
    class-level :py:attr:`protocols` tuple.  The source registry
    (populated in :py:mod:`sources`) walks all subclasses and
    matches source strings against these prefixes.

    Subclasses must implement
    :py:meth:`start`, :py:meth:`stop`, and :py:meth:`read`.
    """

    #: Tuple of protocol prefixes this handler supports (e.g. ``("v4l",)``).
    protocols: tuple[str, ...] = ()

    @abc.abstractmethod
    def start(self, camera: "Camera") -> None:
        """Open the capture device and populate :py:attr:`Camera.max_supported_*`.

        Parameters
        ----------
        camera : Camera
            Camera instance to configure.

        Raises
        ------
        RuntimeError
            If the device cannot be opened.
        """

    @abc.abstractmethod
    def stop(self, camera: "Camera") -> None:
        """Release the capture device and clean up resources.

        Parameters
        ----------
        camera : Camera
            Camera instance to clean up.
        """

    @abc.abstractmethod
    def read(self, camera: "Camera") -> bytes:
        """Read a single frame and populate :py:attr:`Camera.image`.

        The handler reads ``camera.max_width``, ``camera.max_height``,
        and ``camera.max_framerate`` to decide the push size.

        Parameters
        ----------
        camera : Camera
            Camera instance providing user-requested caps and quality.

        Returns
        -------
        bytes
            JPEG-encoded frame data.
        """
