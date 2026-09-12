"""Tests for WebRTC egress: SpiriCamera.webrtc.negotiate_* and the tags channel.

These exercise the exact coroutine both signaling doors call
(``negotiate_async``/``negotiate_sync``) against a real, if local,
``RTCPeerConnection`` acting as the viewer -- there is no mock for
aiortc itself, only for the network being loopback.
"""

from __future__ import annotations

import asyncio
import json

from aiortc import RTCPeerConnection, RTCSessionDescription

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
        await viewer.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type=answer_type)
        )

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
        await viewer.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type=answer_type)
        )

        frame = await asyncio.wait_for(received_frame, 5.0)
        assert frame.width > 0

        await webrtc.close_session_async(session_id)
        await viewer.close()
    finally:
        cam.stop()
