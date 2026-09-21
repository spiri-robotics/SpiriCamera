"""WebRTC plumbing shared by every signaling path.

Two very different callers need to turn an SDP offer into a live
:py:class:`~aiortc.RTCPeerConnection` publishing a
:py:class:`~SpiriCamera.camera.Camera`'s frames:

* :py:meth:`SpiriCamera.camera.Camera.webrtc_offer`, a ``@remote_method``
  invoked synchronously from a zenoh queryable callback thread with no
  event loop of its own.
* :py:mod:`SpiriCamera.whep`'s ``POST /whep`` route, an ``async def``
  running on uvicorn's event loop.

Both end up calling :py:func:`_negotiate` on the *same* background
event loop (:py:func:`get_loop`), which this module starts once, on
first use, and never stops.  That loop is what keeps every peer
connection's ICE/SRTP/media machinery alive after the call that created
it returns -- there is nothing else left holding a reference to it once
a zenoh RPC handler hands back its reply.  ``negotiate_sync`` blocks the
calling thread for the result; ``negotiate_async`` is the awaitable
equivalent for a caller that already has its own loop.  Every session,
however it was opened, lives in the one :py:data:`_SESSIONS` registry,
so closing it works the same way regardless of which door it came in.

Frame tags travel over an ``RTCDataChannel``, but only one way round:
the *viewer* must create the channel (labelled
:py:data:`TAGS_CHANNEL_LABEL`) before sending its offer, because a
single-shot SDP answer can only describe ``m=`` sections the offer
already listed -- an answerer cannot introduce a new one without a
second round of negotiation.  A plain WHEP player that only asks for
video gets video only; a SpiriCamera-aware client that opens the tags
channel in its offer gets one JSON message per frame,
``{"timestamp": ..., "tags": {...}}``, read straight back out of that
frame's own EXIF via :py:func:`SpiriCamera.exif.extract` -- the same
tags :py:meth:`Camera.exif_tags_for_frame
<SpiriCamera.camera.Camera.exif_tags_for_frame>` already baked in, not
a second copy of them.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import Future

import cv2
import numpy as np
from aiortc import (
    RTCDataChannel,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from av import VideoFrame
from loguru import logger

from SpiriCamera import exif

#: Label a viewer's offer must use for its data channel to receive tags.
TAGS_CHANNEL_LABEL = "spiricamera-tags"

#: How long to wait for ICE candidate gathering before answering.
#:
#: This module does not implement trickle ICE (no WHEP ``PATCH``
#: route), so the answer is not sent until every local candidate is
#: known -- simpler, and well within tolerance for a LAN or a host with
#: a reachable public address, at the cost of slower negotiation
#: through a asymmetric NAT.
_ICE_GATHERING_TIMEOUT = 5.0

#: Frame served while a session has no image yet.
_BLANK_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()

#: Every live peer connection, keyed by session id, regardless of
#: whether it was opened by :py:meth:`Camera.webrtc_offer` or
#: :py:mod:`SpiriCamera.whep`.
_SESSIONS: dict[str, RTCPeerConnection] = {}


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the shared background event loop, starting it if needed.

    Every :py:class:`~aiortc.RTCPeerConnection` this module creates
    lives on this loop for its entire life, so it keeps running forever
    in a daemon thread rather than being torn down between calls.
    """
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever,
                name="spiricamera-webrtc",
                daemon=True,
            ).start()
            _loop = loop
        return _loop


def _run(coro: object, timeout: float | None = None) -> Future:
    """Schedule ``coro`` on :py:func:`get_loop` and return its future."""
    return asyncio.run_coroutine_threadsafe(coro, get_loop())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Egress: publishing a Camera's frames to a peer
# ---------------------------------------------------------------------------


class _CameraVideoTrack(VideoStreamTrack):
    """Re-encodes a :py:class:`~SpiriCamera.camera.Camera`'s frames for WebRTC.

    Reads :py:attr:`~SpiriCamera.camera.CameraBase.image` -- already
    JPEG-encoded and EXIF-tagged by ``Camera`` -- on every ``recv()``,
    so this track always shows the same frame every other consumer
    (the debug UI, a mirror) sees, tags included.
    """

    kind = "video"

    def __init__(self, camera: object, tags_channel: dict[str, RTCDataChannel]) -> None:
        super().__init__()
        self._camera = camera
        # A one-entry box rather than a plain attribute: the channel
        # does not exist yet when the track is created, and is filled
        # in later by the peer connection's "datachannel" handler,
        # if the viewer's offer asked for one at all.
        self._tags_channel = tags_channel

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()

        image: bytes = self._camera.image
        array = None
        if image:
            array = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            array = _BLANK_FRAME

        frame = VideoFrame.from_ndarray(array, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base

        channel = self._tags_channel.get("channel")
        if channel is not None and channel.readyState == "open":
            tags = exif.extract(image) if image else {}
            channel.send(
                json.dumps(
                    {"timestamp": tags.get(exif.TIMESTAMP_TAG, ""), "tags": tags}
                )
            )

        return frame


async def _wait_ice_complete(
    pc: RTCPeerConnection, timeout: float = _ICE_GATHERING_TIMEOUT
) -> None:
    """Block until ``pc`` has gathered every local ICE candidate."""
    if pc.iceGatheringState == "complete":
        return

    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    @pc.on("icegatheringstatechange")
    def _on_change() -> None:
        if pc.iceGatheringState == "complete" and not done.done():
            done.set_result(None)

    try:
        await asyncio.wait_for(done, timeout)
    except TimeoutError:
        logger.warning("ICE gathering did not finish within timeout, answering anyway")


async def _negotiate(camera: object, sdp: str, type_: str) -> tuple[str, str, str]:
    """Build a peer connection publishing ``camera`` and answer ``sdp``.

    Returns
    -------
    tuple[str, str, str]
        ``(session_id, answer_sdp, answer_type)``.
    """
    session_id = uuid.uuid4().hex
    pc = RTCPeerConnection()
    _SESSIONS[session_id] = pc

    tags_channel: dict[str, RTCDataChannel] = {}

    @pc.on("datachannel")
    def _on_datachannel(channel: RTCDataChannel) -> None:
        if channel.label == TAGS_CHANNEL_LABEL:
            tags_channel["channel"] = channel

    @pc.on("connectionstatechange")
    async def _on_state_change() -> None:
        if pc.connectionState in ("failed", "closed"):
            await _close_session(session_id)

    pc.addTrack(_CameraVideoTrack(camera, tags_channel))

    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await _wait_ice_complete(pc)

    local = pc.localDescription
    logger.debug(f"webrtc: negotiated session {session_id}")
    return session_id, local.sdp, local.type


def negotiate_sync(
    camera: object, sdp: str, type_: str = "offer", timeout: float = 10.0
) -> tuple[str, str, str]:
    """Synchronous entry point, for a caller with no event loop of its own."""
    return _run(_negotiate(camera, sdp, type_)).result(timeout)


async def negotiate_async(
    camera: object, sdp: str, type_: str = "offer", timeout: float = 10.0
) -> tuple[str, str, str]:
    """Awaitable entry point, for a caller already running one."""
    return await asyncio.wrap_future(_run(_negotiate(camera, sdp, type_)))


async def _close_session(session_id: str) -> bool:
    pc = _SESSIONS.pop(session_id, None)
    if pc is None:
        return False
    await pc.close()
    logger.debug(f"webrtc: closed session {session_id}")
    return True


def close_session_sync(session_id: str, timeout: float = 5.0) -> bool:
    """Close a session by id.  Returns ``False`` if it was already gone."""
    return _run(_close_session(session_id)).result(timeout)


async def close_session_async(session_id: str, timeout: float = 5.0) -> bool:
    """Awaitable equivalent of :py:func:`close_session_sync`."""
    return await asyncio.wrap_future(_run(_close_session(session_id)))


# ---------------------------------------------------------------------------
# Ingest: pulling a remote WHEP publisher's frames
# ---------------------------------------------------------------------------


async def _whep_pull(url: str) -> RTCPeerConnection:
    """Negotiate a recvonly session against a remote WHEP endpoint.

    Returns
    -------
    RTCPeerConnection
        A connected peer connection; the caller reads frames off
        ``pc.getReceivers()[0].track``.
    """
    import httpx

    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await _wait_ice_complete(pc)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            content=pc.localDescription.sdp,
            headers={"Content-Type": "application/sdp"},
            timeout=10.0,
        )
        response.raise_for_status()
        answer_sdp = response.text

    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
    return pc


def pull_sync(url: str, timeout: float = 15.0) -> RTCPeerConnection:
    """Synchronous entry point for :py:func:`_whep_pull`."""
    return _run(_whep_pull(url)).result(timeout)


def close_peer_sync(pc: RTCPeerConnection, timeout: float = 5.0) -> None:
    """Close a peer connection created by :py:func:`pull_sync`."""
    _run(pc.close()).result(timeout)


__all__ = [
    "TAGS_CHANNEL_LABEL",
    "close_peer_sync",
    "close_session_async",
    "close_session_sync",
    "get_loop",
    "negotiate_async",
    "negotiate_sync",
    "pull_sync",
]
