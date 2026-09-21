"""Tests for the WHEP ingest source, against a real local WHEP publisher.

WebRTC needs real UDP sockets for ICE even on loopback, so unlike the
other handler tests this one runs an actual :py:mod:`SpiriCamera.whep`
app on an ephemeral localhost port rather than faking the transport.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from SpiriCamera.sources.base import CaptureSettings
from SpiriCamera.whep import create_whep_app

SETTINGS = CaptureSettings(max_width=160, max_height=120)


@pytest.fixture
def whep_source_url(camera) -> Iterator[str]:
    """Run a real WHEP publisher and return a ``whep+http://`` source string for it."""
    publisher = camera(max_width=160, max_height=120)
    publisher.start()

    config = uvicorn.Config(
        create_whep_app(lambda: publisher),
        host="127.0.0.1",
        port=0,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "WHEP publisher did not start"
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"whep+http://127.0.0.1:{port}/whep"

    server.should_exit = True
    thread.join(timeout=5.0)
    publisher.stop()


def test_pulls_frames_from_whep_publisher(whep_source_url: str, camera) -> None:
    """Opening a ``whep+http://`` source negotiates and starts receiving frames."""
    cam = camera(whep_source_url)
    try:
        cam.start(background=False)

        frame = None
        for _ in range(100):
            frame = cam.read_frame()
            if frame is not None:
                break
            time.sleep(0.1)

        assert frame is not None
        assert frame.ndim == 3
        assert frame.shape[2] == 3
    finally:
        cam.stop()


def test_close_stops_the_pull_task(whep_source_url: str, camera) -> None:
    """Closing releases the peer connection and clears the cached frame."""
    cam = camera(whep_source_url)
    handler = None
    try:
        cam.start(background=False)
        for _ in range(100):
            if cam.read_frame() is not None:
                break
            time.sleep(0.1)
        handler = cam.handler
        assert handler is not None
        assert handler.is_open
    finally:
        cam.stop()

    assert handler is not None
    assert not handler.is_open
    assert handler.read(SETTINGS) is None
