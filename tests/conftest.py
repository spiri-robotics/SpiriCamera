"""Shared fixtures.

Three rules keep this suite fast, hardware-independent, and off the
network:

* Everything a test builds lives in its own zenoh namespace, on a peer
  that talks to nobody.  See :py:data:`SYNQ_NAMESPACE` and
  :py:func:`synq_session`.
* Cameras are built with ``synq_auto_start=False`` so no test declares
  zenoh resources it does not need.  The handful of tests that care
  about sync opt in explicitly.
* Nothing opens a real device.  ``testimage://`` covers the end-to-end
  path for real, and :py:class:`FakeCapture` stands in for
  ``cv2.VideoCapture`` everywhere else.
"""

from __future__ import annotations

import json
import os
import uuid

#: Topic prefix for everything this test run publishes.
#:
#: SpiriSynq prefixes authoritative topics with the hostname, so an
#: unnamespaced run publishes to the very topics a real camera on this
#: machine would — and two developers on one network write over each
#: other.  A fresh name per run also means a crashed run leaves nothing
#: behind for the next one to trip over.
#:
#: This has to be set before SpiriSynq is imported, which is why it sits
#: above the imports: the module-level default session is constructed at
#: import time and reads the environment exactly once.
SYNQ_NAMESPACE = f"spiricamera-test-{uuid.uuid4().hex[:12]}"
os.environ["SPIRI_SYNQ_BASE_TOPIC"] = SYNQ_NAMESPACE

from collections.abc import AsyncGenerator, Callable, Iterator  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import zenoh  # noqa: E402
from nicegui.testing.user import User  # noqa: E402
from nicegui.testing.user_simulation import user_simulation  # noqa: E402
from SpiriSynq.session import Session, current_session  # noqa: E402

from SpiriCamera.camera import Camera  # noqa: E402

#: Zenoh config for the test peer: no discovery, no endpoints, no peers.
#:
#: Namespacing alone keeps the topics apart, but a scouting peer still
#: joins whatever multicast group the developer's machine is on and
#: announces itself to every node there.  A test suite has no business
#: on the network at all, and an isolated peer also means the suite
#: behaves the same on a laptop, in a container, and on a build machine
#: with no network to scout.
ISOLATED_ZENOH_CONFIG = {
    "mode": "peer",
    "scouting": {"multicast": {"enabled": False}, "gossip": {"enabled": False}},
    "listen": {"endpoints": []},
    "connect": {"endpoints": []},
}


@pytest.fixture(scope="session", autouse=True)
def synq_session() -> Iterator[Session]:
    """Make an isolated, namespaced session the default for every test.

    ``SyncableObject`` takes its session from a context variable at
    construction, so setting it here covers every object a test builds
    without any test having to pass ``synq_session=``.

    Yields
    ------
    Session
        The session under test, for the rare test that asserts on it.
    """
    session = Session(
        config=zenoh.Config.from_json5(json.dumps(ISOLATED_ZENOH_CONFIG)),
        base_topic=SYNQ_NAMESPACE,
    )
    token = current_session.set(session)
    try:
        yield session
    finally:
        current_session.reset(token)
        session.close()


class FakeCapture:
    """Stand-in for ``cv2.VideoCapture``.

    Records the properties set on it and hands back frames from a
    scripted list, so tests can drive negotiation and read failures
    without a device.
    """

    def __init__(
        self,
        target: object = None,
        *,
        opened: bool = True,
        width: int = 640,
        height: int = 480,
        framerate: int = 30,
        frames: list[np.ndarray | None] | None = None,
    ) -> None:
        """Create a fake capture.

        Parameters
        ----------
        target : object
            Whatever was passed to ``cv2.VideoCapture``; recorded for
            assertions.
        opened : bool
            Whether the device should report itself as open.
        width, height, framerate : int
            What the device reports back after settings are applied.
        frames : list[np.ndarray | None] | None
            Frames to return from successive reads.  ``None`` entries
            become a failed read.  Exhausting the list repeats the last
            entry.  Defaults to an endless supply of blank frames.
        """
        self.target = target
        self.opened = opened
        self.width = width
        self.height = height
        self.framerate = framerate
        self.properties: dict[int, float] = {}
        self.released = False
        self._frames = (
            frames if frames is not None else [np.zeros((height, width, 3), np.uint8)]
        )
        self._reads = 0

    def isOpened(self) -> bool:  # noqa: N802 - mirrors the cv2 API
        """Whether the device is open."""
        return self.opened and not self.released

    def set(self, prop: int, value: float) -> bool:  # noqa: A003
        """Record a property the source tried to apply."""
        self.properties[prop] = value
        return True

    def get(self, prop: int) -> float:
        """Report the negotiated value of a property."""
        import cv2

        return {
            cv2.CAP_PROP_FRAME_WIDTH: float(self.width),
            cv2.CAP_PROP_FRAME_HEIGHT: float(self.height),
            cv2.CAP_PROP_FPS: float(self.framerate),
        }.get(prop, 0.0)

    def read(self) -> tuple[bool, np.ndarray | None]:
        """Return the next scripted frame."""
        index = min(self._reads, len(self._frames) - 1)
        self._reads += 1
        frame = self._frames[index]
        return (frame is not None), frame

    def release(self) -> None:
        """Mark the device released."""
        self.released = True


class CaptureHolder:
    """Holds the fake capture a source opened, for later assertions."""

    capture: FakeCapture | None = None


@pytest.fixture
def fake_capture(monkeypatch: pytest.MonkeyPatch) -> Callable[..., CaptureHolder]:
    """Patch ``cv2.VideoCapture`` with a scripted fake.

    Returns
    -------
    Callable[..., CaptureHolder]
        Call with :py:class:`FakeCapture` keyword arguments to install
        the behaviour.  The returned holder's ``capture`` attribute is
        populated once the source under test opens one.
    """

    def install(**kwargs: object) -> CaptureHolder:
        holder = CaptureHolder()

        def factory(target: object) -> FakeCapture:
            holder.capture = FakeCapture(target, **kwargs)  # type: ignore[arg-type]
            return holder.capture

        monkeypatch.setattr("SpiriCamera.sources.capture.cv2.VideoCapture", factory)
        return holder

    return install


@pytest.fixture
def camera() -> Iterator[Callable[..., Camera]]:
    """Build cameras that do not touch zenoh, and stop them afterwards.

    Yields
    ------
    Callable[..., Camera]
        Called with the same arguments as :py:class:`Camera`.
    """
    built: list[Camera] = []

    def build(source: str = "testimage://", **kwargs: object) -> Camera:
        kwargs.setdefault("synq_auto_start", False)
        cam = Camera(source, **kwargs)  # type: ignore[arg-type]
        built.append(cam)
        return cam

    yield build

    for cam in built:
        cam.stop()


@pytest.fixture
async def user(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[User, None]:
    """Render the real page against a throwaway camera.

    ``build_page`` is passed as ``root`` so NiceGUI calls the page
    handler directly, sidestepping the decorator's route cache.  The
    process-wide camera is replaced first so the page under test never
    touches a device or the zenoh network.
    """
    from SpiriCamera.ui import build_page

    cam = Camera("testimage://", max_width=160, max_height=120, synq_auto_start=False)
    monkeypatch.setattr("SpiriCamera.ui._camera", cam)

    async with user_simulation(root=build_page) as simulated:
        yield simulated

    cam.stop()
