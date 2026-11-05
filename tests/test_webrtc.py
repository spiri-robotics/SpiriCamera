"""Tests for WebRTC egress: SpiriCamera.webrtc.negotiate_* and the tags channel.

These exercise the exact coroutine both signaling doors call
(``negotiate_async``/``negotiate_sync``) against a real, if local,
``RTCPeerConnection`` acting as the viewer -- there is no mock for
aiortc itself, only for the network being loopback.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.codecs import h264
from aiortc.rtp import RTCP_PSFB_PLI, RtcpPsfbPacket, RtcpReceiverInfo, RtcpRrPacket

from SpiriCamera import webrtc


async def _wait_ice_complete(pc: RTCPeerConnection, timeout: float = 5.0) -> None:
    """Local equivalent of ``webrtc._wait_ice_complete``, for the viewer side."""
    if pc.iceGatheringState == "complete":
        return

    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    @pc.on("icegatheringstatechange")
    def _on_change() -> None:
        if pc.iceGatheringState == "complete" and not done.done():
            done.set_result(None)

    await asyncio.wait_for(done, timeout)


async def test_negotiate_publishes_video_and_tags(camera) -> None:
    """A viewer offering a tags channel gets both video and per-frame tags."""
    cam = camera(max_width=160, max_height=120)
    cam.start()
    try:
        for _ in range(50):
            if cam.image:
                break
            await asyncio.sleep(0.05)
        assert cam.image, "camera never produced a frame"

        viewer = RTCPeerConnection()
        viewer.addTransceiver("video", direction="recvonly")
        tags_channel = viewer.createDataChannel(webrtc.TAGS_CHANNEL_LABEL)

        received_frame: asyncio.Future = asyncio.get_running_loop().create_future()

        @viewer.on("track")
        def _on_track(track: object) -> None:
            async def _consume() -> None:
                frame = await track.recv()  # type: ignore[attr-defined]
                if not received_frame.done():
                    received_frame.set_result(frame)

            asyncio.ensure_future(_consume())

        received_message: asyncio.Future = asyncio.get_running_loop().create_future()
        tags_channel.on(
            "message",
            lambda msg: received_message.done() or received_message.set_result(msg),
        )

        offer = await viewer.createOffer()
        await viewer.setLocalDescription(offer)
        await _wait_ice_complete(viewer)

        session_id, answer_sdp, answer_type = await webrtc.negotiate_async(
            cam, viewer.localDescription.sdp, viewer.localDescription.type
        )
        await viewer.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type=answer_type))

        frame = await asyncio.wait_for(received_frame, 5.0)
        assert frame.width > 0
        assert frame.height > 0

        message = await asyncio.wait_for(received_message, 5.0)
        payload = json.loads(message)
        assert payload["tags"]
        assert payload["tags"]["software"].startswith("SpiriCamera")

        assert await webrtc.close_session_async(session_id)
        assert not await webrtc.close_session_async(session_id)

        await viewer.close()
    finally:
        cam.stop()


async def test_negotiate_without_tags_channel_still_publishes_video(camera) -> None:
    """A plain WHEP-style viewer with no data channel still gets video."""
    cam = camera(max_width=160, max_height=120)
    cam.start()
    try:
        for _ in range(50):
            if cam.image:
                break
            await asyncio.sleep(0.05)

        viewer = RTCPeerConnection()
        viewer.addTransceiver("video", direction="recvonly")

        received_frame: asyncio.Future = asyncio.get_running_loop().create_future()

        @viewer.on("track")
        def _on_track(track: object) -> None:
            async def _consume() -> None:
                frame = await track.recv()  # type: ignore[attr-defined]
                if not received_frame.done():
                    received_frame.set_result(frame)

            asyncio.ensure_future(_consume())

        offer = await viewer.createOffer()
        await viewer.setLocalDescription(offer)
        await _wait_ice_complete(viewer)

        session_id, answer_sdp, answer_type = await webrtc.negotiate_async(
            cam, viewer.localDescription.sdp, viewer.localDescription.type
        )
        assert "m=application" not in answer_sdp
        await viewer.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type=answer_type))

        frame = await asyncio.wait_for(received_frame, 5.0)
        assert frame.width > 0

        await webrtc.close_session_async(session_id)
        await viewer.close()
    finally:
        cam.stop()


# ---------------------------------------------------------------------------
# Rate control
# ---------------------------------------------------------------------------


def test_rate_controller_cuts_on_loss_and_grows_when_clean() -> None:
    rate = webrtc._RateController()
    start = rate.estimate

    rate.on_loss(0.3)
    assert rate.estimate == pytest.approx(start * 0.85)

    cut = rate.estimate
    rate.on_loss(0.05)  # between the thresholds: hold
    assert rate.estimate == cut

    rate.on_loss(0.0)
    assert rate.estimate == pytest.approx(cut * webrtc._GROWTH)


def test_rate_controller_stays_inside_codec_limits() -> None:
    """Never asks the encoder for more than the ceiling."""
    rate = webrtc._RateController()
    for _ in range(100):
        rate.on_loss(0.0)
    assert rate.target == h264.MAX_BITRATE
    assert rate.fps == webrtc._MAX_FPS
    assert rate.max_height is None


def test_rate_controller_drops_frame_rate_below_h264_floor() -> None:
    """Below the floor the encoder target stays put and frame rate falls."""
    rate = webrtc._RateController()
    rate.estimate = h264.MIN_BITRATE / 3
    rate.on_loss(0.05)
    assert rate.target == h264.MIN_BITRATE
    assert rate.fps == pytest.approx(webrtc._MAX_FPS / 3)
    assert rate.max_height == 480

    for _ in range(100):
        rate.on_loss(1.0)
    assert rate.fps == pytest.approx(webrtc._MIN_FPS)


def test_rate_controller_treats_viewer_remb_as_ceiling() -> None:
    rate = webrtc._RateController()
    rate.on_remb(700_000)
    for _ in range(100):
        rate.on_loss(0.0)
    assert rate.target == 700_000


# ---------------------------------------------------------------------------
# The one shared encode
# ---------------------------------------------------------------------------


async def _started(camera) -> object:
    cam = camera(max_width=160, max_height=120)
    cam.start()
    for _ in range(50):
        if cam.image:
            break
        await asyncio.sleep(0.05)
    assert cam.image, "camera never produced a frame"
    return cam


async def _view(cam: object, codec: str = "video/H264") -> tuple[RTCPeerConnection, str]:
    """Connect a viewer offering only ``codec``; returns it once a frame arrives."""
    viewer = RTCPeerConnection()
    viewer.addTransceiver("video", direction="recvonly").setCodecPreferences(
        [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType == codec]
    )
    received: asyncio.Future = asyncio.get_running_loop().create_future()

    @viewer.on("track")
    def _on_track(track: object) -> None:
        async def _consume() -> None:
            while True:
                frame = await track.recv()  # type: ignore[attr-defined]
                if not received.done():
                    received.set_result(frame)

        asyncio.ensure_future(_consume())

    offer = await viewer.createOffer()
    await viewer.setLocalDescription(offer)
    await _wait_ice_complete(viewer)
    session_id, answer_sdp, answer_type = await webrtc.negotiate_async(
        cam, viewer.localDescription.sdp, viewer.localDescription.type
    )
    await viewer.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type=answer_type))
    await asyncio.wait_for(received, 5.0)
    return viewer, session_id


async def _inject_loss(session_id: str, fraction_lost: int) -> None:
    """Hand ``session_id``'s sender a receiver report, as its viewer would."""
    sender = webrtc._SESSIONS[session_id].getSenders()[0]
    report = RtcpReceiverInfo(
        ssrc=sender._ssrc,
        fraction_lost=fraction_lost,
        packets_lost=0,
        highest_sequence=0,
        jitter=0,
        lsr=0,
        dlsr=0,
    )
    await asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(
            sender._handle_rtcp_packet(RtcpRrPacket(ssrc=0, reports=[report])),
            webrtc.get_loop(),
        )
    )


async def test_every_session_shares_one_encode(camera) -> None:
    """Two viewers of a camera both get video, from one encoder."""
    cam = await _started(camera)
    try:
        first, first_id = await _view(cam)
        second, second_id = await _view(cam)
        assert webrtc._SESSION_ENCODERS[first_id] is webrtc._SESSION_ENCODERS[second_id]
        assert [key for key in webrtc._ENCODERS if key == id(cam)] == [id(cam)]

        await webrtc.close_session_async(first_id)
        assert id(cam) in webrtc._ENCODERS
        await webrtc.close_session_async(second_id)
        assert id(cam) not in webrtc._ENCODERS
        await first.close()
        await second.close()
    finally:
        cam.stop()


async def test_the_encode_suits_the_worst_session(camera) -> None:
    """One lossy viewer lowers the shared encoder's bitrate for both."""
    cam = await _started(camera)
    try:
        good, good_id = await _view(cam)
        bad, bad_id = await _view(cam)
        encoder = webrtc._SESSION_ENCODERS[good_id]

        good_rate, bad_rate = webrtc._RATES[good_id], webrtc._RATES[bad_id]
        good_rate.on_remb(h264.MAX_BITRATE)
        good_rate.estimate = h264.MAX_BITRATE
        bad_rate.on_remb(800_000)  # a ceiling no report can grow past

        # A new encoder only once the target moves 10%, as aiortc's does.
        ceiling = 800_000 * 1.1
        for _ in range(100):
            codec = encoder._codec
            if codec is not None and codec.bit_rate <= ceiling:
                break
            await asyncio.sleep(0.05)
        assert encoder._codec.bit_rate <= ceiling < good_rate.target

        for session_id in (good_id, bad_id):
            await webrtc.close_session_async(session_id)
        await good.close()
        await bad.close()
    finally:
        cam.stop()


async def test_session_adapts_to_receiver_reports(camera) -> None:
    """A lossy receiver report lowers that session's rate."""
    cam = await _started(camera)
    try:
        viewer, session_id = await _view(cam)
        for _ in range(20):
            await _inject_loss(session_id, 255)  # all lost
        assert webrtc._RATES[session_id].fps < webrtc._MAX_FPS
        await webrtc.close_session_async(session_id)
        await viewer.close()
    finally:
        cam.stop()


async def test_a_keyframe_request_goes_to_the_shared_encode() -> None:
    """aiortc would hand PLI to the session's own encoder, which isn't used."""
    handled: list[object] = []
    requests: list[None] = []

    class Sender:
        _ssrc = 1234

        async def _handle_rtcp_packet(self, packet: object) -> None:
            handled.append(packet)

    rate = webrtc._RateController()
    sender = Sender()
    rate.install(sender, lambda: requests.append(None))  # type: ignore[arg-type]
    try:
        await sender._handle_rtcp_packet(RtcpPsfbPacket(fmt=RTCP_PSFB_PLI, ssrc=0, media_ssrc=1234))
        assert requests == [None]
        assert handled == []
    finally:
        rate.stop()


async def test_a_mirror_cannot_publish(camera) -> None:
    """A mirror's own session would be a second encode of the camera."""
    cam = camera("testimage://", synq_authoritive=False)
    viewer = RTCPeerConnection()
    viewer.addTransceiver("video", direction="recvonly")
    offer = await viewer.createOffer()
    await viewer.setLocalDescription(offer)
    sessions = dict(webrtc._SESSIONS)
    with pytest.raises(ValueError, match="mirror"):
        await webrtc.negotiate_async(cam, viewer.localDescription.sdp, "offer")
    assert sessions == webrtc._SESSIONS
    await viewer.close()


async def test_an_offer_without_h264_is_refused(camera) -> None:
    cam = await _started(camera)
    try:
        sessions = dict(webrtc._SESSIONS)
        with pytest.raises(ValueError, match="H.264"):
            await _view(cam, codec="video/VP8")
        assert sessions == webrtc._SESSIONS
        assert id(cam) not in webrtc._ENCODERS
    finally:
        cam.stop()


def test_h264_preset_must_be_x264_s(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from SpiriCamera.main import Settings

    monkeypatch.setenv("SPIRICAMERA_H264_PRESET", "ultrafast")
    assert Settings().h264_preset == "ultrafast"
    monkeypatch.setenv("SPIRICAMERA_H264_PRESET", "speedy")
    with pytest.raises(ValidationError):
        Settings()
