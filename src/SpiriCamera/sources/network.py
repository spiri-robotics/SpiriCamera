"""Network camera source (RTSP, RTMP, HTTP, HTTPS)."""

from __future__ import annotations

from SpiriCamera.sources.base import SourceError, SourceURL
from SpiriCamera.sources.capture import OpenCVSource


class NetworkSource(OpenCVSource):
    """Remote video stream opened through OpenCV's FFmpeg backend.

    RTSP, RTMP, and plain HTTP(S) streams share the same capture
    lifecycle; only the URL differs, so one handler covers all four.
    """

    schemes = ("rtsp", "rtmp", "http", "https")

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
