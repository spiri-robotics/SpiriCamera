"""Video files and still images on disk.

OpenCV opens both through the same ``VideoCapture``: a video yields its
frames and then stops, a still yields exactly one frame and then stops.
Rewinding at that point covers both — a video loops, and a still becomes
a steady picture — so one handler serves either without having to know
which it was given.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from loguru import logger

from SpiriCamera.sources.base import CaptureSettings, SourceError, SourceURL
from SpiriCamera.sources.capture import OpenCVSource


class FileSource(OpenCVSource):
    """A video file or still image, played on a loop.

    Written either as a plain path (``/srv/clips/approach.mp4``,
    ``./still.png``) or with an explicit scheme (``file:///srv/x.mp4``).

    The explicit form exists because a path is not always distinguishable
    from a URL.  POSIX collapses repeated slashes, so ``rtsp://clip.mp4``
    is a perfectly valid path to ``clip.mp4`` inside a directory named
    ``rtsp:`` — and it would otherwise be read as a stream URL.  Anything
    ambiguous can be forced with ``file://``.
    """

    schemes = ("file",)

    def __init__(self, url: SourceURL, *, loop: bool = True) -> None:
        """Create a file source.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.
        loop : bool
            Whether to rewind at the end of the file.
        """
        super().__init__(url)
        self._loop = loop
        self._frames_seen = 0
        self._first_frame: np.ndarray | None = None
        self._still_frame: np.ndarray | None = None

    @classmethod
    def handles(cls, url: SourceURL) -> bool:
        """Claim ``file://`` URLs and schemeless paths to real files.

        A bare number is a camera index by long-standing OpenCV
        convention, so it is left to the V4L2 handler even if a file of
        that name happens to exist.  ``file://0`` reaches the file.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.

        Returns
        -------
        bool
            True if this is a file this handler should open.
        """
        if url.scheme in cls.schemes:
            return True
        if url.scheme or url.target.isdigit():
            return False
        return _is_regular_file(url.target)

    @classmethod
    def from_url(cls, url: SourceURL) -> FileSource:
        """Check the path is a readable file before claiming it.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.

        Returns
        -------
        FileSource
            A handler for the file.

        Raises
        ------
        SourceError
            If the path is missing, is not a regular file, or cannot be
            read.
        """
        path = Path(url.target)
        if not url.target:
            raise SourceError(f"No file given in {url.raw!r}")
        if not path.exists():
            raise SourceError(f"No such file: {url.target!r}")
        if not path.is_file():
            raise SourceError(f"Not a regular file: {url.target!r}")
        return cls(url)

    def capture_target(self) -> str:
        """Hand OpenCV the filesystem path, without any scheme."""
        return self.url.target

    def close(self) -> None:
        """Release the file and forget where we had got to."""
        super().close()
        self._frames_seen = 0
        self._first_frame = None
        self._still_frame = None

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        """Read the next frame, looping at the end of the file.

        Parameters
        ----------
        settings : CaptureSettings
            Unused; the file decides its own dimensions.

        Returns
        -------
        np.ndarray | None
            The next frame, or ``None`` if the file yielded nothing at
            all.
        """
        if self._still_frame is not None:
            return self._still_frame

        frame = super().read(settings)
        if frame is not None:
            self._frames_seen += 1
            if self._first_frame is None:
                self._first_frame = frame
            return frame

        if not self._loop:
            return None

        # A file that gave up exactly one frame is a still. Seeking will
        # not help: OpenCV reports the rewind succeeded and then refuses
        # to read, so hold on to the frame already in hand. Returning the
        # identical array also lets the camera reuse its encode.
        if self._frames_seen <= 1 and self._first_frame is not None:
            logger.debug(f"{self!r} is a single frame; serving it as a still")
            self._still_frame = self._first_frame
            return self._still_frame

        # One rewind per read, so an unreadable file cannot spin here.
        if not self._rewind():
            return None
        frame = super().read(settings)
        if frame is not None:
            self._frames_seen = 1
        return frame

    def _rewind(self) -> bool:
        """Seek back to the first frame."""
        if self._capture is None:
            return False
        logger.debug(f"{self!r} rewinding")
        return bool(self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0))


def _is_regular_file(path: str) -> bool:
    """Whether a path names a regular file, without raising."""
    try:
        return Path(path).is_file()
    except OSError:
        return False
