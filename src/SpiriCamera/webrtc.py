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

Every published session adapts its send rate to the link on its own,
from the packet loss the viewer reports -- see :py:class:`_RateController`.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import Future

import cv2
import numpy as np
from aiortc import (
    RTCDataChannel,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.codecs import h264, vpx
from aiortc.mediastreams import VIDEO_CLOCK_RATE, VIDEO_TIME_BASE
from aiortc.rtp import (
    RTCP_PSFB_APP,
    RtcpPsfbPacket,
    RtcpRrPacket,
    RtcpSrPacket,
    pack_remb_fci,
    unpack_remb_fci,
)
from aiortc.sdp import SessionDescription
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

#: Frame rate a session publishes at while the link has room.  Matches
#: aiortc's own ``MAX_FRAME_RATE``, which its H.264 encoder assumes when
#: budgeting bits per frame.
_MAX_FPS = 30.0

#: Lowest frame rate :py:class:`_RateController` will drop to.
_MIN_FPS = 2.0

#: Per-codec ``(start, floor, ceiling, budgets_per_frame)``, in bps.
#:
#: Floor and ceiling are aiortc's own, which its encoders clamp every
#: ``target_bitrate`` to; we stay inside them.  ``budgets_per_frame``
#: says whether sending fewer frames cuts bandwidth below the floor:
#: libx264 here spends ``target_bitrate / 30`` per frame however many
#: frames arrive, so 10 fps at a 500 kbps target sends ~160 kbps;
#: libvpx runs true CBR, holding 250 kbps at any frame rate.
_CODEC_RATES: dict[str, tuple[int, int, int, bool]] = {
    "video/h264": (h264.DEFAULT_BITRATE, h264.MIN_BITRATE, h264.MAX_BITRATE, True),
    "video/vp8": (vpx.DEFAULT_BITRATE, vpx.MIN_BITRATE, vpx.MAX_BITRATE, False),
}

#: ``(minimum encoder target, frame height cap)``, first match wins:
#: a lower target is spent on fewer, cleaner pixels.
_HEIGHT_CAPS: tuple[tuple[int, int | None], ...] = (
    (1_500_000, None),
    (800_000, 720),
    (0, 480),
)

#: Receiver-reported loss above which the estimate is cut, and below
#: which it grows -- the loss-based half of Google Congestion Control.
_LOSS_HIGH = 0.10
_LOSS_LOW = 0.02
_GROWTH = 1.08

#: Seconds without a receiver report, once they have started arriving,
#: before the link is treated as stalled and the estimate halved.  A
#: viewer sends one roughly every second, over the same link the media
#: crosses, so silence means that link is in trouble.
_STALL_TIMEOUT = 3.0

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()

#: Every live peer connection, keyed by session id, regardless of
#: whether it was opened by :py:meth:`Camera.webrtc_offer` or
#: :py:mod:`SpiriCamera.whep`.
_SESSIONS: dict[str, RTCPeerConnection] = {}

#: Each live session's :py:class:`_RateController`, by session id.
_RATES: dict[str, _RateController] = {}


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


class _RateController:
    """Per-session send-rate estimate, fed to aiortc as REMB.

    aiortc only ever adapts its encoder to a REMB message from the
    viewer.  Plenty of viewers never send one -- go2rtc (and so
    Frigate) advertises ``goog-remb`` but doesn't generate it -- so
    without this the stream sits at the codec's starting bitrate on any
    link.  Every viewer does send RTCP receiver reports, though, and
    those carry the fraction of packets lost since the last one.

    :py:meth:`install` wraps the sender's RTCP handler so each report
    moves the estimate (cut on heavy loss, grown on clean reports,
    halved if reports stop), and the estimate is handed back into that
    same handler as a REMB packet, so aiortc's own REMB path is the one
    that sets ``target_bitrate``.  A REMB the viewer sends itself is
    swallowed and kept as a ceiling on the estimate instead, so the two
    don't overwrite each other.

    REMB can't push the encoder outside aiortc's clamp, and nothing
    here tries to.  Below the floor, the estimate is instead met by
    lowering :py:attr:`fps` -- for H.264 only, see :py:data:`_CODEC_RATES`
    -- and the frame is capped to :py:attr:`max_height` to suit the
    encoder target.  :py:class:`_CameraVideoTrack` reads both.
    """

    def __init__(self, mime_type: str = "video/H264") -> None:
        #: Latest bitrate the viewer itself sent as REMB, if any.
        self.remb: int | None = None
        self._last_report: float | None = None
        self._watchdog: asyncio.Task | None = None
        self.set_codec(mime_type)

    def set_codec(self, mime_type: str) -> None:
        """Reset to ``mime_type``'s rates, once negotiation has picked it."""
        start, self.floor, self.ceiling, self.budgets_per_frame = _CODEC_RATES.get(
            mime_type.lower(), _CODEC_RATES["video/h264"]
        )
        #: Estimated bitrate the link will carry, in bps.
        self.estimate = float(start)
        self._update_outputs()

    @property
    def target(self) -> int:
        """Encoder target: the estimate, clamped as aiortc will clamp it."""
        return int(max(self.floor, min(self.estimate, self.ceiling)))

    def on_loss(self, fraction_lost: float) -> None:
        """Fold one receiver report's loss fraction (0..1) into the estimate."""
        self._last_report = time.monotonic()
        if fraction_lost > _LOSS_HIGH:
            self.estimate *= 1 - 0.5 * fraction_lost
        elif fraction_lost < _LOSS_LOW:
            self.estimate *= _GROWTH
        self._update_outputs()

    def on_remb(self, bitrate: int) -> None:
        """Record a REMB the viewer sent as a ceiling on the estimate."""
        self.remb = bitrate
        self._update_outputs()

    def on_stall(self) -> None:
        """Halve the estimate after reports stop arriving."""
        self._last_report = time.monotonic()
        self.estimate /= 2
        self._update_outputs()

    def _update_outputs(self) -> None:
        upper = self.ceiling if self.remb is None else min(self.ceiling, self.remb)
        lower = self.floor * (_MIN_FPS / _MAX_FPS if self.budgets_per_frame else 1)
        self.estimate = max(lower, min(self.estimate, upper))
        target = self.target
        self.fps = _MAX_FPS * min(1.0, self.estimate / target)
        self.max_height = next(cap for minimum, cap in _HEIGHT_CAPS if target >= minimum)

    def install(self, sender: RTCRtpSender) -> None:
        """Hook ``sender``'s RTCP handling and start the stall watchdog."""
        original = sender._handle_rtcp_packet

        async def _send_remb() -> None:
            fci = pack_remb_fci(self.target, [sender._ssrc])
            await original(RtcpPsfbPacket(fmt=RTCP_PSFB_APP, ssrc=0, media_ssrc=0, fci=fci))

        async def _handle_rtcp_packet(packet: object) -> None:
            if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_APP:
                try:
                    bitrate, ssrcs = unpack_remb_fci(packet.fci)
                except ValueError:
                    bitrate, ssrcs = 0, []
                if sender._ssrc in ssrcs:
                    self.on_remb(bitrate)
                    await _send_remb()
                    return

            await original(packet)

            if isinstance(packet, (RtcpRrPacket, RtcpSrPacket)):
                for report in packet.reports:
                    if report.ssrc == sender._ssrc:
                        # RTCP carries the fraction as 8-bit fixed point.
                        self.on_loss(report.fraction_lost / 256)
                        await _send_remb()

        async def _watchdog() -> None:
            while True:
                await asyncio.sleep(_STALL_TIMEOUT / 3)
                if (
                    self._last_report is not None
                    and time.monotonic() - self._last_report > _STALL_TIMEOUT
                ):
                    self.on_stall()
                    logger.debug(f"webrtc: no receiver report, estimate {self.estimate:.0f}")
                    await _send_remb()

        sender._handle_rtcp_packet = _handle_rtcp_packet  # type: ignore[method-assign]
        self._watchdog = asyncio.ensure_future(_watchdog())

    def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()


class _CameraVideoTrack(VideoStreamTrack):
    """Re-encodes a :py:class:`~SpiriCamera.camera.Camera`'s frames for WebRTC.

    Reads :py:attr:`~SpiriCamera.camera.CameraBase.image` -- already
    JPEG-encoded and EXIF-tagged by ``Camera`` -- on every ``recv()``,
    so this track always shows the same frame every other consumer
    (the debug UI, a mirror) sees, tags included.  Frames are paced at,
    and shrunk to, whatever the session's :py:class:`_RateController`
    currently allows.
    """

    kind = "video"

    def __init__(
        self,
        camera: object,
        tags_channel: dict[str, RTCDataChannel],
        rate: _RateController,
    ) -> None:
        super().__init__()
        self._camera = camera
        # A one-entry box rather than a plain attribute: the channel
        # does not exist yet when the track is created, and is filled
        # in later by the peer connection's "datachannel" handler,
        # if the viewer's offer asked for one at all.
        self._tags_channel = tags_channel
        self._rate = rate
        self._started: float | None = None
        self._due = 0.0

    async def next_timestamp(self) -> tuple[int, object]:
        """Like :py:class:`VideoStreamTrack`'s, at ``self._rate.fps``."""
        now = time.monotonic()
        if self._started is None:
            self._started = self._due = now
        else:
            self._due += 1 / self._rate.fps
            if self._due > now:
                await asyncio.sleep(self._due - now)
            else:
                # Fell behind; carry on from now rather than bursting.
                self._due = now
        return int((self._due - self._started) * VIDEO_CLOCK_RATE), VIDEO_TIME_BASE

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()

        image: bytes = self._camera.image
        array = None
        if image:
            array = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            array = _BLANK_FRAME

        cap = self._rate.max_height
        if cap is not None and array.shape[0] > cap:
            # yuv420p, which every encoder converts to, needs even sides.
            width = round(array.shape[1] * cap / array.shape[0] / 2) * 2
            array = cv2.resize(array, (width, cap), interpolation=cv2.INTER_AREA)

        frame = VideoFrame.from_ndarray(array, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base

        channel = self._tags_channel.get("channel")
        if channel is not None and channel.readyState == "open":
            tags = exif.extract(image) if image else {}
            channel.send(json.dumps({"timestamp": tags.get(exif.TIMESTAMP_TAG, ""), "tags": tags}))

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

    rate = _RateController()
    _RATES[session_id] = rate
    sender = pc.addTrack(_CameraVideoTrack(camera, tags_channel, rate))
    # H.264 first, whatever order the offer lists: a relay like MediaMTX offers
    # VP8 first and passes on what it gets untouched, and its viewers (Spiri
    # Cloud's Frigate among them) may only take H.264. Other codecs stay
    # behind it for viewers that can't do H.264.
    codecs = RTCRtpSender.getCapabilities("video").codecs
    for transceiver in pc.getTransceivers():
        if transceiver.sender is sender:
            transceiver.setCodecPreferences(
                sorted(codecs, key=lambda c: c.mimeType.lower() != "video/h264")
            )

    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await _wait_ice_complete(pc)

    # The sender encodes with the answer's first video codec.
    for media in SessionDescription.parse(pc.localDescription.sdp).media:
        if media.kind == "video" and media.rtp.codecs:
            rate.set_codec(media.rtp.codecs[0].mimeType)
            break
    rate.install(sender)

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
    rate = _RATES.pop(session_id, None)
    if rate is not None:
        rate.stop()
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
