"""Tests for the WHEP ingest source, against a real local WHEP publisher.

WebRTC needs real UDP sockets for ICE even on loopback, so unlike the
other handler tests this one runs an actual :py:mod:`SpiriCamera.whep`
app on an ephemeral localhost port rather than faking the transport.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
import uvicorn

from SpiriCamera.camera import Camera
from SpiriCamera.sources.base import CaptureSettings
from SpiriCamera.whep import create_whep_app

SETTINGS = CaptureSettings(max_width=160, max_height=120)


@contextlib.contextmanager
def _serving(get_camera: Callable[[], object]) -> Iterator[str]:
    """Run a real WHEP app for ``get_camera`` and return a ``whep+http://`` source for it."""
    config = uvicorn.Config(
        create_whep_app(get_camera),
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

    try:
        yield f"whep+http://127.0.0.1:{port}/whep"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
def whep_source_url(camera) -> Iterator[str]:
    """Run a real WHEP publisher and return a ``whep+http://`` source string for it."""
    publisher = camera(max_width=160, max_height=120)
    publisher.start()
    try:
        with _serving(lambda: publisher) as url:
            yield url
    finally:
        publisher.stop()


def _first_frame(cam: object) -> object:
    for _ in range(100):
        frame = cam.read_frame()  # type: ignore[attr-defined]
        if frame is not None:
            return frame
        time.sleep(0.1)
    return None


def test_pulls_frames_from_whep_publisher(whep_source_url: str, camera) -> None:
    """Opening a ``whep+http://`` source negotiates and starts receiving frames."""
    cam = camera(whep_source_url)
    try:
        cam.start(background=False)
        frame = _first_frame(cam)
        assert frame is not None
        assert frame.ndim == 3
        assert frame.shape[2] == 3
    finally:
        cam.stop()


def test_whep_over_a_mirror_signals_for_the_camera(camera, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mirror can't encode, so its WHEP app's video comes from the camera's process."""
    publisher = camera(
        max_width=160,
        max_height=120,
        synq_topic=f"whep_mirrored_{uuid.uuid4().hex[:8]}",
        synq_auto_start=True,
    )
    publisher.start()
    # The real Camera: other tests on this shared session replace the
    # !Camera constructor through from_topic_untyped (see
    # test_spirisynq_source's test_leaves_the_camera_type_registration_alone).
    session = publisher.synq_session
    constructors = session.type_registry.constructor.yaml_constructors
    monkeypatch.setitem(constructors, "!Camera", constructors.get("!Camera"))
    session.type_registry.register_class(Camera)
    mirror = Camera.from_topic(publisher.synq_absolute_path)
    try:
        assert not mirror.synq_authoritive
        with _serving(lambda: mirror) as url:
            cam = camera(url)
            try:
                cam.start(background=False)
                assert _first_frame(cam) is not None
            finally:
                cam.stop()
    finally:
        mirror.close()
        publisher.stop()


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


def test_whep_answers_options() -> None:
    """OPTIONS /whep succeeds: MediaMTX's WHEP client asks first and gives up on an error."""
    from fastapi.testclient import TestClient

    client = TestClient(create_whep_app(lambda: None))
    response = client.options("/whep")
    assert response.status_code == 204
    assert "POST" in response.headers["Allow"]
