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
from aiortc.codecs import h264, vpx
from aiortc.rtp import RtcpReceiverInfo, RtcpRrPacket

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
    rate = webrtc._RateController("video/H264")
    start = rate.estimate

    rate.on_loss(0.3)
    assert rate.estimate == pytest.approx(start * 0.85)

    cut = rate.estimate
    rate.on_loss(0.05)  # between the thresholds: hold
    assert rate.estimate == cut

    rate.on_loss(0.0)
    assert rate.estimate == pytest.approx(cut * webrtc._GROWTH)


def test_rate_controller_stays_inside_codec_limits() -> None:
    """Never asks the encoder for more than aiortc's ceiling."""
    rate = webrtc._RateController("video/H264")
    for _ in range(100):
        rate.on_loss(0.0)
    assert rate.target == h264.MAX_BITRATE
    assert rate.fps == webrtc._MAX_FPS
    assert rate.max_height is None


def test_rate_controller_drops_frame_rate_below_h264_floor() -> None:
    """Below the floor the encoder target stays put and frame rate falls."""
    rate = webrtc._RateController("video/H264")
    rate.estimate = h264.MIN_BITRATE / 3
    rate.on_loss(0.05)
    assert rate.target == h264.MIN_BITRATE
    assert rate.fps == pytest.approx(webrtc._MAX_FPS / 3)
    assert rate.max_height == 480

    for _ in range(100):
        rate.on_loss(1.0)
    assert rate.fps == pytest.approx(webrtc._MIN_FPS)


def test_rate_controller_vp8_holds_frame_rate_at_floor() -> None:
    """libvpx is CBR, so fewer frames would not save anything."""
    rate = webrtc._RateController("video/VP8")
    for _ in range(100):
        rate.on_loss(1.0)
    assert rate.target == vpx.MIN_BITRATE
    assert rate.fps == webrtc._MAX_FPS


def test_rate_controller_treats_viewer_remb_as_ceiling() -> None:
    rate = webrtc._RateController("video/H264")
    rate.on_remb(700_000)
    for _ in range(100):
        rate.on_loss(0.0)
    assert rate.target == 700_000


async def test_session_adapts_encoder_to_receiver_reports(camera) -> None:
    """A lossy receiver report reaches the encoder via aiortc's REMB path."""
    cam = camera(max_width=160, max_height=120)
    cam.start()
    try:
        for _ in range(50):
            if cam.image:
                break
            await asyncio.sleep(0.05)

        viewer = RTCPeerConnection()
        # H.264, as go2rtc/Frigate negotiate; aiortc would pick VP8.
        viewer.addTransceiver("video", direction="recvonly").setCodecPreferences(
            [
                codec
                for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType == "video/H264"
            ]
        )
        received_frame: asyncio.Future = asyncio.get_running_loop().create_future()

        @viewer.on("track")
        def _on_track(track: object) -> None:
            async def _consume() -> None:
                while True:
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
        await viewer.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type=answer_type))
        await asyncio.wait_for(received_frame, 5.0)

        rate = webrtc._RATES[session_id]
        sender = webrtc._SESSIONS[session_id].getSenders()[0]

        def encoder() -> object:
            # Test-only peek at aiortc's private encoder.
            return sender._RTCRtpSender__encoder

        async def inject(fraction_lost: int) -> None:
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

        for _ in range(20):
            await inject(255)  # all lost
        assert rate.fps < webrtc._MAX_FPS
        assert encoder().target_bitrate == rate.target

        await webrtc.close_session_async(session_id)
        await viewer.close()
    finally:
        cam.stop()
