"""Network camera source (RTSP, RTMP, HTTP, HTTPS)."""

from __future__ import annotations

import time

import cv2
import numpy as np

from SpiriCamera.sources.base import CaptureSettings, SourceCapabilities, SourceError, SourceURL
from SpiriCamera.sources.capture import OpenCVSource

#: How long a stream may go without delivering a frame before it is
#: treated as lost and reopened.  Also FFmpeg's read timeout, which
#: otherwise defaults to 30 seconds per read.
_STALL_TIMEOUT = 5.0

#: How long FFmpeg may take to connect to a stream.
_OPEN_TIMEOUT = 10.0


class NetworkSource(OpenCVSource):
    """Remote video stream opened through OpenCV's FFmpeg backend.

    RTSP, RTMP, and plain HTTP(S) streams share the same capture
    lifecycle; only the URL differs, so one handler covers all four.
    """

    schemes = ("rtsp", "rtmp", "http", "https")

    def __init__(self, url: SourceURL) -> None:
        super().__init__(url)
        #: When the stream last delivered a frame on time.
        self._last_frame_at = 0.0

    @classmethod
    def from_url(cls, url: SourceURL) -> NetworkSource:
        """Reject stream URLs with no host part.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.

        Returns
        -------
        NetworkSource
            A handler for the stream.

        Raises
        ------
        SourceError
            If the URL carries no host.
        """
        if not url.target:
            raise SourceError(f"No host given in stream URL {url.raw!r}")
        return cls(url)

    def capture_target(self) -> str:
        """Hand OpenCV the full stream URL, scheme included."""
        return self.url.raw

    def capture_params(self) -> list[int]:
        """Bound how long FFmpeg blocks connecting to, and reading, the stream."""
        return [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            int(_OPEN_TIMEOUT * 1000),
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            int(_STALL_TIMEOUT * 1000),
        ]

    def open(self, settings: CaptureSettings) -> SourceCapabilities:
        """Open the stream, starting its stall clock -- see :py:meth:`read`."""
        capabilities = super().open(settings)
        self._last_frame_at = time.monotonic()
        return capabilities

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        """Grab a frame, closing the source if the stream has stalled.

        A stream whose server dropped the session -- an RTSP server does
        once its client stops reading for long enough -- never recovers
        on the same capture object: every read waits out the timeout,
        and the first few still hand back stale frames from the
        decoder.  So a frame only counts as live if it arrived before
        the timeout, and a stream with none for :py:data:`_STALL_TIMEOUT`
        closes itself; the camera then reconnects.

        Raises
        ------
        SourceError
            If the stream has delivered no live frame for too long.
        """
        started = time.monotonic()
        frame = super().read(settings)
        now = time.monotonic()
        if frame is not None and now - started < _STALL_TIMEOUT:
            self._last_frame_at = now
        elif now - self._last_frame_at >= _STALL_TIMEOUT:
            self.close()
            raise SourceError(
                f"Stream {self.url.raw!r} sent no frame for "
                f"{now - self._last_frame_at:.0f}s, reconnecting"
            )
        return frame
