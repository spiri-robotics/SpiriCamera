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

A camera is encoded once, however many sessions publish it: one
:py:class:`_SharedEncoder` per camera, H.264 only, whose packets every
session's track hands to aiortc as they are.  Each session still
estimates its own link from the packet loss its viewer reports (see
:py:class:`_RateController`), and the encode suits the worst of them.
Only the camera's own process can publish it: a mirror would be a
second encode, so :py:func:`negotiate_sync` refuses one, and a mirror's
:py:meth:`~SpiriCamera.camera.Camera.webrtc_offer` reaches the camera
itself over SpiriSynq.
"""

from __future__ import annotations

import asyncio
import fractions
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future

import av
import cv2
import numpy as np
from aiortc import (
    MediaStreamTrack,
    RTCDataChannel,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.codecs import h264
from aiortc.exceptions import OperationError
from aiortc.mediastreams import VIDEO_CLOCK_RATE, VIDEO_TIME_BASE
from aiortc.rtp import (
    RTCP_PSFB_APP,
    RTCP_PSFB_FIR,
    RTCP_PSFB_PLI,
    RtcpPsfbPacket,
    RtcpRrPacket,
    RtcpSrPacket,
    unpack_remb_fci,
)
from aiortc.sdp import SessionDescription
from av import Packet, VideoFrame
from av.video.frame import PictureType
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

#: Frame rate the encode runs at while every link has room, and the one
#: :py:class:`_SharedEncoder` tells libx264 it runs at, whatever it does:
#: libx264 then spends ``target_bitrate / 30`` per frame however many
#: frames arrive, so 10 fps at a 500 kbps target sends ~160 kbps.  That
#: is how a session gets below the bitrate floor.
_MAX_FPS = 30.0

#: Lowest frame rate :py:class:`_RateController` will drop to.
_MIN_FPS = 2.0

#: ``(start, floor, ceiling)`` of the encoder's target, in bps: aiortc's
#: own H.264 encoder's, which this encode keeps to.
_START_BITRATE, _MIN_BITRATE, _MAX_BITRATE = (
    h264.DEFAULT_BITRATE,
    h264.MIN_BITRATE,
    h264.MAX_BITRATE,
)

#: Encoded frames a session may fall behind by before it is cut off and
#: resynced at the next keyframe: a second, at full rate.
_QUEUE_FRAMES = 30

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

#: Each publishing camera's one :py:class:`_SharedEncoder`, by ``id()``.
_ENCODERS: dict[int, _SharedEncoder] = {}

#: The encoder each live session takes its packets from, by session id.
_SESSION_ENCODERS: dict[str, _SharedEncoder] = {}


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
    """Per-session estimate of what the viewer's link will carry.

    Plenty of viewers never send REMB -- go2rtc (and so Frigate)
    advertises ``goog-remb`` but doesn't generate it -- so a stream
    waiting on one would sit at its starting bitrate on any link.  Every
    viewer does send RTCP receiver reports, though, and those carry the
    fraction of packets lost since the last one.

    :py:meth:`install` wraps the sender's RTCP handler so each report
    moves the estimate (cut on heavy loss, grown on clean reports,
    halved if reports stop).  A REMB the viewer sends itself is kept as
    a ceiling on the estimate, and a keyframe request (PLI/FIR) is
    passed on to the shared encode; aiortc would only hand either to an
    encoder of its own, which a session no longer has.

    The encoder target stays between :py:data:`_MIN_BITRATE` and
    :py:data:`_MAX_BITRATE`.  Below the floor, the estimate is instead
    met by lowering :py:attr:`fps`, and the frame is capped to
    :py:attr:`max_height` to suit the target.  :py:class:`_SharedEncoder`
    reads all three, from every session, and encodes for the worst.
    """

    def __init__(self) -> None:
        #: Latest bitrate the viewer itself sent as REMB, if any.
        self.remb: int | None = None
        self.floor = _MIN_BITRATE
        self.ceiling = _MAX_BITRATE
        #: Estimated bitrate the link will carry, in bps.
        self.estimate = float(_START_BITRATE)
        self._last_report: float | None = None
        self._watchdog: asyncio.Task | None = None
        self._update_outputs()

    @property
    def target(self) -> int:
        """Encoder target: the estimate, clamped to the floor and ceiling."""
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
        lower = self.floor * _MIN_FPS / _MAX_FPS
        self.estimate = max(lower, min(self.estimate, upper))
        target = self.target
        self.fps = _MAX_FPS * min(1.0, self.estimate / target)
        self.max_height = next(cap for minimum, cap in _HEIGHT_CAPS if target >= minimum)

    def install(self, sender: RTCRtpSender, request_keyframe: Callable[[], None]) -> None:
        """Hook ``sender``'s RTCP handling and start the stall watchdog."""
        original = sender._handle_rtcp_packet

        async def _handle_rtcp_packet(packet: object) -> None:
            if isinstance(packet, RtcpPsfbPacket):
                if packet.fmt == RTCP_PSFB_APP:
                    try:
                        bitrate, ssrcs = unpack_remb_fci(packet.fci)
                    except ValueError:
                        bitrate, ssrcs = 0, []
                    if sender._ssrc in ssrcs:
                        self.on_remb(bitrate)
                        return
                elif packet.fmt in (RTCP_PSFB_PLI, RTCP_PSFB_FIR):
                    request_keyframe()
                    return

            await original(packet)

            if isinstance(packet, (RtcpRrPacket, RtcpSrPacket)):
                for report in packet.reports:
                    if report.ssrc == sender._ssrc:
                        # RTCP carries the fraction as 8-bit fixed point.
                        self.on_loss(report.fraction_lost / 256)

        async def _watchdog() -> None:
            while True:
                await asyncio.sleep(_STALL_TIMEOUT / 3)
                if (
                    self._last_report is not None
                    and time.monotonic() - self._last_report > _STALL_TIMEOUT
                ):
                    self.on_stall()
                    logger.debug(f"webrtc: no receiver report, estimate {self.estimate:.0f}")

        sender._handle_rtcp_packet = _handle_rtcp_packet  # type: ignore[method-assign]
        self._watchdog = asyncio.ensure_future(_watchdog())

    def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()


class _Subscriber:
    """One session's share of a camera's :py:class:`_SharedEncoder`."""

    def __init__(self, rate: _RateController, tags_channel: dict[str, RTCDataChannel]) -> None:
        self.rate = rate
        # A one-entry box rather than a plain attribute: the channel
        # does not exist yet when the session is created, and is filled
        # in later by the peer connection's "datachannel" handler,
        # if the viewer's offer asked for one at all.
        self.tags_channel = tags_channel
        #: Encoded frames, with their tags, waiting for this session's sender.
        self.queue: asyncio.Queue[tuple[Packet, dict]] = asyncio.Queue(_QUEUE_FRAMES)
        #: Whether this session has had a keyframe since it joined or fell
        #: behind; until it has, the frames before one are useless to it.
        self.synced = False

    @property
    def wants_tags(self) -> bool:
        channel = self.tags_channel.get("channel")
        return channel is not None and channel.readyState == "open"


class _SharedEncoder:
    """A camera's one H.264 encode, shared by every session publishing it.

    Reads :py:attr:`~SpiriCamera.camera.CameraBase.image` -- already
    JPEG-encoded and EXIF-tagged by ``Camera`` -- at the encode's frame
    rate, so viewers see the same frame every other consumer (the debug
    UI, a mirror) sees, tags included.  Each frame is encoded once, by
    libx264 at ``SPIRICAMERA_H264_PRESET``, and its packets go to every
    session's queue; aiortc only packetizes them.

    The encode suits the worst viewer: the lowest target bitrate, frame
    rate and height cap among the sessions' :py:class:`_RateController`.
    A session that joins, falls :py:data:`_QUEUE_FRAMES` behind, or asks
    for one (PLI/FIR) gets a keyframe, and is sent nothing until then.
    """

    def __init__(self, camera: object) -> None:
        from SpiriCamera.main import get_settings

        self._camera = camera
        self._preset = get_settings().h264_preset
        self._subscribers: dict[str, _Subscriber] = {}
        self._codec: av.CodecContext | None = None
        self._frames = 0
        self._keyframe = True
        self._task: asyncio.Task | None = None
        # An encode still running in its executor when the last session
        # leaves must not overlap one for the next session to join.
        self._encoding = threading.Lock()

    def subscribe(self, session_id: str, subscriber: _Subscriber) -> None:
        self._subscribers[session_id] = subscriber
        self.request_keyframe()
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    def unsubscribe(self, session_id: str) -> bool:
        """Drop a session.  Returns whether that was the last one."""
        self._subscribers.pop(session_id, None)
        if self._subscribers:
            return False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._codec = None
        return True

    def request_keyframe(self) -> None:
        self._keyframe = True

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        started = due = time.monotonic()
        while True:
            rates = [subscriber.rate for subscriber in self._subscribers.values()]
            fps = min(rate.fps for rate in rates)
            target = min(rate.target for rate in rates)
            caps = [rate.max_height for rate in rates if rate.max_height is not None]

            due += 1 / fps
            now = time.monotonic()
            if due > now:
                await asyncio.sleep(due - now)
            else:
                # Fell behind; carry on from now rather than bursting.
                due = now

            image: bytes = self._camera.image  # type: ignore[attr-defined]
            keyframe, self._keyframe = self._keyframe, False
            pts = int((due - started) * VIDEO_CLOCK_RATE)
            packets = await loop.run_in_executor(
                None, self._encode, image, target, min(caps, default=None), keyframe, pts
            )
            subscribers = list(self._subscribers.values())
            tags = exif.extract(image) if image and any(s.wants_tags for s in subscribers) else {}
            for subscriber in subscribers:
                self._deliver(subscriber, packets, tags)

    def _deliver(self, subscriber: _Subscriber, packets: list[Packet], tags: dict) -> None:
        for packet in packets:
            if not subscriber.synced:
                if not packet.is_keyframe:
                    continue
                subscriber.synced = True
            try:
                subscriber.queue.put_nowait((packet, tags))
            except asyncio.QueueFull:
                # Too far behind to catch up: start it again from a keyframe.
                while not subscriber.queue.empty():
                    subscriber.queue.get_nowait()
                subscriber.synced = False
                self.request_keyframe()
                return

    def _encode(
        self, image: bytes, target: int, cap: int | None, keyframe: bool, pts: int
    ) -> list[Packet]:
        """Encode one frame; runs in an executor, off the event loop."""
        with self._encoding:
            return self._encode_locked(image, target, cap, keyframe, pts)

    def _encode_locked(
        self, image: bytes, target: int, cap: int | None, keyframe: bool, pts: int
    ) -> list[Packet]:
        array = None
        if image:
            array = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            array = _BLANK_FRAME
        if cap is not None and array.shape[0] > cap:
            width = round(array.shape[1] * cap / array.shape[0])
            array = cv2.resize(array, (width, cap), interpolation=cv2.INTER_AREA)
        # yuv420p needs even sides.
        height, width = array.shape[0] // 2 * 2, array.shape[1] // 2 * 2
        array = array[:height, :width]

        codec = self._codec
        if (
            codec is None
            or (codec.width, codec.height) != (width, height)
            or abs(target - codec.bit_rate) / codec.bit_rate > 0.1
        ):
            # A new encoder, as aiortc's own does on a new size or a
            # changed bitrate; its first frame is a keyframe.
            codec = self._codec = av.CodecContext.create("libx264", "w")
            codec.width, codec.height = width, height
            codec.bit_rate = target
            codec.pix_fmt = "yuv420p"
            codec.framerate = fractions.Fraction(int(_MAX_FPS), 1)
            codec.time_base = fractions.Fraction(1, int(_MAX_FPS))
            # aiortc's own options, plus the preset it leaves at x264's
            # default (medium), far too slow for live video on a Pi.
            codec.options = {"level": "31", "tune": "zerolatency", "preset": self._preset}
            codec.profile = "Baseline"

        frame = VideoFrame.from_ndarray(array, format="bgr24")
        # Counted, not timed: see _MAX_FPS.
        frame.pts = self._frames
        self._frames += 1
        frame.pict_type = PictureType.I if keyframe else PictureType.NONE

        packets = list(codec.encode(frame))
        for packet in packets:
            packet.pts = pts
            packet.time_base = VIDEO_TIME_BASE
        return packets


class _CameraVideoTrack(MediaStreamTrack):
    """One session's view of a camera's :py:class:`_SharedEncoder`.

    Hands aiortc already-encoded packets, which it only packetizes, and
    sends each frame's tags down the tags channel, if the viewer opened
    one.
    """

    kind = "video"

    def __init__(self, subscriber: _Subscriber) -> None:
        super().__init__()
        self._subscriber = subscriber

    async def recv(self) -> Packet:
        packet, tags = await self._subscriber.queue.get()
        if self._subscriber.wants_tags:
            self._subscriber.tags_channel["channel"].send(
                json.dumps({"timestamp": tags.get(exif.TIMESTAMP_TAG, ""), "tags": tags})
            )
        return packet


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
    if not getattr(camera, "synq_authoritive", True):
        raise ValueError(
            f"{getattr(camera, 'synq_topic', camera)}: a mirror can't publish WebRTC, it would "
            "be a second encode of the camera; ask the camera itself (Camera.webrtc_offer)"
        )

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
    subscriber = _Subscriber(rate, tags_channel)
    sender = pc.addTrack(_CameraVideoTrack(subscriber))
    # H.264 only, the one encode every session shares.  A relay like MediaMTX
    # offers VP8 first and passes on what it gets untouched, and its viewers
    # (Spiri Cloud's Frigate among them) may only take H.264 anyway.
    codecs = [
        codec
        for codec in RTCRtpSender.getCapabilities("video").codecs
        if codec.mimeType.lower() in ("video/h264", "video/rtx")
    ]
    for transceiver in pc.getTransceivers():
        if transceiver.sender is sender:
            transceiver.setCodecPreferences(codecs)

    try:
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        except OperationError as exc:
            raise ValueError(f"{exc}: this camera publishes H.264 only") from exc
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await _wait_ice_complete(pc)
        video = [
            m for m in SessionDescription.parse(pc.localDescription.sdp).media if m.kind == "video"
        ]
        if not any(c.mimeType.lower() == "video/h264" for m in video for c in m.rtp.codecs):
            raise ValueError("the offer has no H.264, the only codec this camera publishes")
    except Exception:
        await _close_session(session_id)
        raise

    encoder = _ENCODERS.get(id(camera))
    if encoder is None:
        encoder = _ENCODERS[id(camera)] = _SharedEncoder(camera)
    _SESSION_ENCODERS[session_id] = encoder
    encoder.subscribe(session_id, subscriber)
    rate.install(sender, encoder.request_keyframe)

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
    encoder = _SESSION_ENCODERS.pop(session_id, None)
    if encoder is not None and encoder.unsubscribe(session_id):
        for key, value in list(_ENCODERS.items()):
            if value is encoder:
                del _ENCODERS[key]
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
