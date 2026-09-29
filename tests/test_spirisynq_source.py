"""Tests for the ``spirisynq://`` source, against a real upstream camera.

The upstream is an ordinary ``testimage://`` camera publishing on the
suite's isolated session, so this exercises the real SpiriSynq path --
rehydrate, live ``image`` updates -- rather than a fake of it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import pytest
from SpiriSynq.session import current_session

from SpiriCamera import exif
from SpiriCamera.camera import (
    STATUS_RUNNING,
    STATUS_WAITING,
    Camera,
    CameraError,
)
from SpiriCamera.sources import resolve_source, spirisynq
from SpiriCamera.sources.base import SourceError
from SpiriCamera.sources.spirisynq import SpiriSynqSource, fit_within

CameraFactory = Callable[..., Camera]


def wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    """Poll until ``predicate`` is truthy, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting for condition")
        time.sleep(0.02)


@pytest.fixture
def upstream(camera: CameraFactory) -> Camera:
    """A running 640x480 test pattern, published on the test session.

    On a topic of its own: every ``testimage://`` camera defaults to the
    same one, and a proxy must not pick up frames from another test's.
    """
    cam = camera(
        "testimage://",
        synq_topic=f"spirisynq_upstream_{uuid.uuid4().hex[:8]}",
        max_width=640,
        max_height=480,
        max_framerate=30,
        synq_auto_start=True,
    )
    cam.start()
    wait_for(lambda: cam.image)
    return cam


@pytest.fixture
def proxy(camera: CameraFactory, upstream: Camera) -> Callable[..., Camera]:
    """Build a started camera whose source is ``upstream``."""

    def build(**kwargs: object) -> Camera:
        cam = camera(f"spirisynq://{upstream.synq_absolute_path}", **kwargs)
        cam.start(background=False)
        return cam

    return build


class TestResolution:
    """``spirisynq://`` source strings."""

    def test_resolves_to_handler(self) -> None:
        handler = resolve_source("spirisynq://some-host/spiricamera_dev_video0")
        assert isinstance(handler, SpiriSynqSource)
        assert handler.target == "some-host/spiricamera_dev_video0"

    @pytest.mark.parametrize("source", ["spirisynq://", "spirisynq:///"])
    def test_rejects_a_missing_topic(self, source: str) -> None:
        with pytest.raises(SourceError, match="No topic"):
            resolve_source(source)

    def test_scripted_start_refuses_an_unpublished_topic(
        self, camera: CameraFactory
    ) -> None:
        cam = camera("spirisynq://nobody-home/spiricamera_nothing")
        with pytest.raises(CameraError, match="nothing is published"):
            cam.start(background=False)
        assert not cam.running


class TestFitWithin:
    """The downscale-only fitting rule."""

    @pytest.mark.parametrize(
        ("size", "bounds", "expected"),
        [
            ((640, 480), (320, 320), (320, 240)),
            ((640, 480), (1920, 120), (160, 120)),
            ((640, 480), (1920, 1080), (640, 480)),
            ((640, 480), (1, 1), (1, 1)),
        ],
    )
    def test_fits(
        self,
        size: tuple[int, int],
        bounds: tuple[int, int],
        expected: tuple[int, int],
    ) -> None:
        assert fit_within(*size, *bounds) == expected


class TestFrames:
    """Frames flowing from an upstream camera into a proxy."""

    def test_downscales_to_the_proxy_bounds(self, proxy: Callable[..., Camera]) -> None:
        cam = proxy(max_width=320, max_height=320)
        cam.read()
        assert (cam.received_width, cam.received_height) == (320, 240)

    def test_does_not_upscale(self, proxy: Callable[..., Camera]) -> None:
        cam = proxy(max_width=1920, max_height=1080)
        cam.read()
        assert (cam.received_width, cam.received_height) == (640, 480)

    def test_resize_is_live(self, proxy: Callable[..., Camera]) -> None:
        cam = proxy(max_width=640, max_height=480)
        cam.read()
        cam.max_width = 160
        cam.read()
        assert (cam.received_width, cam.received_height) == (160, 120)

    def test_follows_new_upstream_frames(
        self, proxy: Callable[..., Camera], upstream: Camera
    ) -> None:
        cam = proxy()
        first = cam.read_frame()
        upstream.max_width = 320
        wait_for(lambda: cam.read_frame().shape[1] == 320)
        assert first is not None and first.shape[1] == 640

    def test_reuses_the_frame_until_something_changes(
        self, proxy: Callable[..., Camera], upstream: Camera
    ) -> None:
        upstream.stop()
        cam = proxy()
        assert cam.read_frame() is cam.read_frame()

    def test_reports_upstream_identity(
        self, proxy: Callable[..., Camera], upstream: Camera
    ) -> None:
        cam = proxy()
        assert cam.vendor == upstream.vendor == "SpiriCamera"
        assert cam.model == upstream.model

    def test_close_unsubscribes(self, proxy: Callable[..., Camera]) -> None:
        cam = proxy()
        handler = cam.handler
        assert isinstance(handler, SpiriSynqSource)
        cam.stop()
        assert not handler.is_open
        with pytest.raises(SourceError, match="not open"):
            handler.read(cam.capture_settings())


class TestTags:
    """Upstream frame metadata surviving the re-encode."""

    def test_inherits_upstream_tags(
        self, proxy: Callable[..., Camera], upstream: Camera
    ) -> None:
        upstream.exif_set_tags("gps", {"gps_latitude": "45.5017"})
        wait_for(lambda: "gps_latitude" in upstream.exif_tags)
        cam = proxy()
        wait_for(lambda: "gps_latitude" in exif.extract(cam.read()))
        assert cam.exif_tags["gps_latitude"] == "45.5017"

    def test_keeps_the_upstream_capture_time(
        self, proxy: Callable[..., Camera]
    ) -> None:
        cam = proxy()
        cam.read()
        handler = cam.handler
        assert handler is not None
        inherited = handler.frame_tags()[exif.TIMESTAMP_TAG]
        assert cam.exif_tags[exif.TIMESTAMP_TAG] == inherited

    def test_names_itself_not_the_upstream(
        self, proxy: Callable[..., Camera], upstream: Camera
    ) -> None:
        cam = proxy()
        cam.read()
        assert cam.exif_tags["source"] == cam.source_str
        assert cam.exif_tags["topic"] == cam.synq_absolute_path
        assert cam.exif_tags["topic"] != upstream.synq_absolute_path


def test_leaves_the_camera_type_registration_alone(
    proxy: Callable[..., Camera],
    upstream: Camera,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consuming a camera must not re-register its type as a generic class.

    Other tests on this shared session (anything rendering
    ``camera_metrics_widget``) already replace the ``!Camera``
    constructor through ``from_topic_untyped``, and SpiriSynq caches the
    generic class it built, so a later call would not register it again.
    Both are reset here, for this test only, so it sees whether *this*
    source clobbers the registration regardless of what ran before it.
    """
    session = upstream.synq_session
    constructors = session.type_registry.constructor.yaml_constructors
    # setitem with the current value only records it, for restoring after.
    monkeypatch.setitem(constructors, "!Camera", constructors.get("!Camera"))
    monkeypatch.setattr(session, "_generic_classes", {})
    session.type_registry.register_class(Camera)
    before = constructors["!Camera"]

    proxy()

    assert constructors["!Camera"] is before


class TestRetryingTopics:
    """A proxy waits for its upstream topic, and finds it again when lost."""

    @pytest.fixture(autouse=True)
    def fast_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("SpiriCamera.camera._SOURCE_RETRY_INTERVAL", 0.05)
        monkeypatch.setattr(SpiriSynqSource, "STALE_AFTER", 0.2)

    @staticmethod
    def publish(camera: CameraFactory, topic: str) -> Camera:
        """Start a 320x240 test pattern publishing on ``topic``."""
        cam = camera(
            "testimage://",
            synq_topic=topic,
            max_width=320,
            max_height=240,
            max_framerate=30,
            synq_auto_start=True,
        )
        cam.start()
        wait_for(lambda: cam.image)
        return cam

    @staticmethod
    def proxy_of(camera: CameraFactory, topic: str) -> Camera:
        """A background proxy of whatever is (or will be) at ``topic``."""
        cam = camera(f"spirisynq://{cam_path(topic)}")
        cam.start()
        return cam

    def test_waits_for_a_topic_that_appears_later(self, camera: CameraFactory) -> None:
        topic = f"later_{uuid.uuid4().hex[:8]}"
        proxy = self.proxy_of(camera, topic)
        assert proxy.running is True
        assert proxy.status.startswith(STATUS_WAITING)
        assert "nothing is published" in proxy.status

        self.publish(camera, topic)

        wait_for(lambda: proxy.status == STATUS_RUNNING and proxy.received_width)
        assert proxy.received_width == 320

    def test_reconnects_after_the_upstream_closes(self, camera: CameraFactory) -> None:
        topic = f"restarted_{uuid.uuid4().hex[:8]}"
        first = self.publish(camera, topic)
        proxy = self.proxy_of(camera, topic)
        wait_for(lambda: proxy.status == STATUS_RUNNING and proxy.image)

        first.close()
        wait_for(lambda: proxy.status.startswith(STATUS_WAITING))
        assert proxy.running is True

        second = self.publish(camera, topic)
        second.max_width = 160
        wait_for(lambda: proxy.status == STATUS_RUNNING and proxy.received_width == 160)

    def test_reconnects_after_the_upstream_goes_silent(
        self, camera: CameraFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashed upstream sends no tombstone; silence plus absence is enough."""
        topic = f"crashed_{uuid.uuid4().hex[:8]}"
        upstream = self.publish(camera, topic)
        proxy = self.proxy_of(camera, topic)
        wait_for(lambda: proxy.status == STATUS_RUNNING and proxy.image)

        vanished = {"yes": True}
        real_find = spirisynq.find_published
        monkeypatch.setattr(
            spirisynq,
            "find_published",
            lambda session, t: None if vanished["yes"] else real_find(session, t),
        )
        upstream.stop()
        wait_for(lambda: proxy.status.startswith(STATUS_WAITING))

        vanished["yes"] = False
        upstream.start()
        wait_for(lambda: proxy.status == STATUS_RUNNING)

    def test_keeps_a_stopped_upstream(self, camera: CameraFactory) -> None:
        """Stopped is not gone: it still answers, so the mirror is kept."""
        topic = f"paused_{uuid.uuid4().hex[:8]}"
        upstream = self.publish(camera, topic)
        proxy = self.proxy_of(camera, topic)
        wait_for(lambda: proxy.status == STATUS_RUNNING and proxy.image)
        handler = proxy.handler
        assert isinstance(handler, SpiriSynqSource)

        upstream.stop()
        time.sleep(0.6)

        assert proxy.status == STATUS_RUNNING
        assert handler.is_open


def cam_path(topic: str) -> str:
    """Absolute path a camera on the test session publishes ``topic`` at."""
    return f"{current_session.get().base_topic}/{topic}"
