"""Shared fixtures.

Two rules keep this suite fast and hardware-independent:

* Cameras are built with ``synq_auto_start=False`` so no test declares
  zenoh resources it does not need.  The handful of tests that care
  about sync opt in explicitly.
* Nothing opens a real device.  ``testimage://`` covers the end-to-end
  path for real, and :py:class:`FakeCapture` stands in for
  ``cv2.VideoCapture`` everywhere else.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterator

import numpy as np
import pytest
from nicegui.testing.user import User
from nicegui.testing.user_simulation import user_simulation

from SpiriCamera.camera import Camera


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
        self._frames = frames if frames is not None else [np.zeros((height, width, 3), np.uint8)]
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

    cam = Camera(
        "testimage://", max_width=160, max_height=120, synq_auto_start=False
    )
    monkeypatch.setattr("SpiriCamera.ui._camera", cam)

    async with user_simulation(root=build_page) as simulated:
        yield simulated

    cam.stop()
