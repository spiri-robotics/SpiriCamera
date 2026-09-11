"""Tests for video files and still images on disk."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from SpiriCamera.camera import Camera
from SpiriCamera.sources import resolve_source
from SpiriCamera.sources.base import CaptureSettings, SourceError, SourceURL
from SpiriCamera.sources.file import FileSource

SETTINGS = CaptureSettings(max_width=640, max_height=480, max_framerate=30)

#: Frames in the generated clip; small enough to read past the end quickly.
CLIP_FRAMES = 8


@pytest.fixture
def still(tmp_path: Path) -> Path:
    """Write a still image to disk."""
    path = tmp_path / "still.png"
    cv2.imwrite(str(path), np.full((120, 160, 3), 60, np.uint8))
    return path


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    """Write a short video to disk, skipping if no encoder is available."""
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (160, 120)
    )
    if not writer.isOpened():
        pytest.skip("no mp4v encoder available")
    for index in range(CLIP_FRAMES):
        writer.write(np.full((120, 160, 3), index * 20, np.uint8))
    writer.release()
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("video encoding produced no file")
    return path


class TestClaims:
    """Which strings the file handler takes."""

    def test_claims_an_existing_file(self, still: Path) -> None:
        """A plain path to a real file is a file source."""
        assert isinstance(resolve_source(str(still)), FileSource)

    def test_claims_a_relative_path(
        self, still: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files are just files, including relative ones."""
        monkeypatch.chdir(still.parent)
        assert isinstance(resolve_source(still.name), FileSource)
        assert isinstance(resolve_source(f"./{still.name}"), FileSource)

    def test_claims_the_file_scheme(self, still: Path) -> None:
        """An explicit file:// URL is always a file."""
        source = resolve_source(f"file://{still}")
        assert isinstance(source, FileSource)
        assert source.target == str(still)

    def test_declines_a_bare_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare number is a camera index even if such a file exists.

        The OpenCV convention predates this handler, so the index wins;
        ``file://0`` is how you reach a file called ``0``.
        """
        (tmp_path / "0").write_bytes(b"")
        monkeypatch.chdir(tmp_path)

        assert not FileSource.handles(SourceURL.parse("0"))
        assert isinstance(resolve_source("file://0"), FileSource)

    def test_declines_a_missing_path(self, tmp_path: Path) -> None:
        """Nothing claims a path that is not there."""
        assert not FileSource.handles(SourceURL.parse(str(tmp_path / "nope.mp4")))

    def test_declines_a_directory(self, tmp_path: Path) -> None:
        """A directory is not a video source."""
        assert not FileSource.handles(SourceURL.parse(str(tmp_path)))

    def test_rejects_a_directory_by_scheme(self, tmp_path: Path) -> None:
        """Asking for a directory explicitly says why it cannot work."""
        with pytest.raises(SourceError, match="Not a regular file"):
            resolve_source(f"file://{tmp_path}")


class TestStillImage:
    """Serving a single image."""

    def test_reports_the_image_size(self, still: Path) -> None:
        """Capabilities come from the file itself."""
        capabilities = resolve_source(str(still)).open(SETTINGS)
        assert capabilities.max_supported_width == 160
        assert capabilities.max_supported_height == 120

    def test_serves_the_frame_repeatedly(self, still: Path) -> None:
        """A still keeps yielding its one frame rather than running dry.

        OpenCV hands out a single frame and then reports end of file, so
        without a rewind the camera would stall after one read.
        """
        source = resolve_source(str(still))
        source.open(SETTINGS)

        frames = [source.read(SETTINGS) for _ in range(4)]

        assert all(frame is not None for frame in frames)
        assert all(frame.shape == (120, 160, 3) for frame in frames)  # type: ignore[union-attr]


class TestVideoFile:
    """Playing a video."""

    def test_reports_the_files_framerate(self, clip: Path) -> None:
        """A video's own framerate is what it can supply."""
        capabilities = resolve_source(str(clip)).open(SETTINGS)
        assert capabilities.max_supported_framerate == 10
        assert capabilities.max_supported_width == 160

    def test_loops_past_the_end(self, clip: Path) -> None:
        """Reading beyond the last frame rewinds instead of stopping."""
        source = resolve_source(str(clip))
        source.open(SETTINGS)

        frames = [source.read(SETTINGS) for _ in range(CLIP_FRAMES * 2 + 2)]

        assert all(frame is not None for frame in frames)

    def test_stops_at_the_end_when_not_looping(self, clip: Path) -> None:
        """Looping is a choice, not a law."""
        source = FileSource(SourceURL.parse(str(clip)), loop=False)
        source.open(SETTINGS)

        for _ in range(CLIP_FRAMES):
            source.read(SETTINGS)

        assert source.read(SETTINGS) is None


class TestThroughCamera:
    """A file driving a camera end to end."""

    def test_captures_from_a_file(self, clip: Path) -> None:
        """A camera pointed at a file behaves like any other camera."""
        cam = Camera(str(clip), synq_auto_start=False)
        try:
            cam.start(background=False)
            data = cam.read()

            assert data.startswith(b"\xff\xd8")
            assert (cam.received_width, cam.received_height) == (160, 120)
            assert cam.source.handler == "FileSource"
        finally:
            cam.stop()

    def test_runs_longer_than_the_clip(self, clip: Path) -> None:
        """The loop keeps a short clip playing indefinitely."""
        cam = Camera(str(clip), synq_auto_start=False)
        try:
            cam.start(background=False)
            for _ in range(CLIP_FRAMES * 2):
                cam.read()
            assert cam.image
        finally:
            cam.stop()
