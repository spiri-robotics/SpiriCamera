"""WHEP camera source: pulling a remote WebRTC publisher's frames.

This is the "receiving" half of WebRTC support -- the mirror image of
:py:mod:`SpiriCamera.whep`, which does the "sending" half.  A
``whep+http://`` or ``whep+https://`` source string is a WHEP
publisher's HTTP endpoint (its own :py:mod:`SpiriCamera.whep` app,
or any other WHEP-compliant server); opening it negotiates a recvonly
session via :py:mod:`SpiriCamera.webrtc` and keeps a background task
on that module's shared event loop pulling decoded frames off the
resulting track.
"""

from __future__ import annotations

import threading

import numpy as np
from aiortc import RTCPeerConnection
from loguru import logger

from SpiriCamera import webrtc
from SpiriCamera.sources.base import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceURL,
)


class WHEPSource(SourceBase):
    """Reads frames from a remote WHEP publisher.

    ``open()`` does the whole WHEP handshake (offer, POST, answer) up
    front; nothing about ``settings`` changes a running session, since
    a WHEP publisher -- not this handler -- decides what it sends.
    """

    #: Compound schemes, so the inner URL keeps its own ``http``/``https``
    #: meaning instead of colliding with it -- ``NetworkSource`` already
    #: claims bare ``http``/``https`` for OpenCV streaming.
    schemes = ("whep+http", "whep+https")

    def __init__(self, url: SourceURL) -> None:
        super().__init__(url)
        self._pc: RTCPeerConnection | None = None
        self._lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._stopped = threading.Event()

    @classmethod
    def from_url(cls, url: SourceURL) -> WHEPSource:
        if not url.target:
            raise SourceError(f"No host given in WHEP URL {url.raw!r}")
        return cls(url)

    def _endpoint(self) -> str:
        """The real ``http(s)://`` URL this compound scheme stands for."""
        scheme = "https" if self.url.scheme == "whep+https" else "http"
        return f"{scheme}://{self.url.target}"

    @property
    def is_open(self) -> bool:
        return self._pc is not None

    def open(self, settings: CaptureSettings) -> SourceCapabilities:
        del settings  # A WHEP publisher sends what it sends.
        if self._pc is not None:
            return SourceCapabilities()

        endpoint = self._endpoint()
        try:
            pc = webrtc.pull_sync(endpoint)
        except Exception as exc:
            raise SourceError(f"could not negotiate WHEP session with {endpoint}: {exc}") from exc

        receivers = pc.getReceivers()
        if not receivers:
            webrtc.close_peer_sync(pc)
            raise SourceError(f"{endpoint} answered with no media track")

        self._pc = pc
        self._stopped.clear()
        webrtc.get_loop().call_soon_threadsafe(
            lambda: webrtc.get_loop().create_task(self._pull_frames(receivers[0].track))
        )
        return SourceCapabilities()

    async def _pull_frames(self, track: object) -> None:
        """Background task, running on ``webrtc.get_loop()``: decode every frame."""
        while not self._stopped.is_set():
            try:
                frame = await track.recv()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug(f"{self.url.raw}: WHEP track ended: {exc}")
                return
            array = frame.to_ndarray(format="bgr24")
            with self._lock:
                self._latest_frame = array

    def close(self) -> None:
        self._stopped.set()
        pc, self._pc = self._pc, None
        if pc is not None:
            webrtc.close_peer_sync(pc)
        with self._lock:
            self._latest_frame = None

    def read(self, settings: CaptureSettings) -> np.ndarray | None:
        del settings
        with self._lock:
            return self._latest_frame
