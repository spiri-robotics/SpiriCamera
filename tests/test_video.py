"""The H.264 encode in :py:mod:`SpiriCamera.video`."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from SpiriCamera.rtsp import split_annexb
from SpiriCamera.transports import TransportVideo
from SpiriCamera.video import VideoEncoder

_FPS = 30
_TARGET = 300_000
_WIDTH, _HEIGHT = 320, 180


def _encoder() -> VideoEncoder:
    camera = SimpleNamespace(transport_video=TransportVideo(), max_framerate=_FPS)
    return VideoEncoder(camera)  # type: ignore[arg-type]


def _bits(encoder: VideoEncoder, frames: list[np.ndarray]) -> int:
    total = 0
    for index, frame in enumerate(frames):
        keyframe = index % (2 * _FPS) == 0
        for payload, *_ in encoder._encode(frame, _TARGET, None, keyframe, 0.0):
            total += len(payload) * 8
    return total


def _slices(encoder: VideoEncoder, frames: list[np.ndarray]) -> list[int]:
    """The size of every slice of every frame encoded."""
    sizes = []
    for index, frame in enumerate(frames):
        keyframe = index % (2 * _FPS) == 0
        for payload, *_ in encoder._encode(frame, _TARGET, None, keyframe, 0.0):
            sizes += [len(unit) for unit in split_annexb(payload) if unit[0] & 0x1F in (1, 5)]
    return sizes


def _noise(count: int) -> list[np.ndarray]:
    rng = np.random.default_rng(0)
    return [rng.integers(0, 255, (_HEIGHT, _WIDTH, 3), dtype=np.uint8) for _ in range(count)]


class TestSlices:
    def test_no_slice_is_bigger_than_slice_max_size(self) -> None:
        encoder = _encoder()
        encoder._camera.transport_video.slice_max_size = 1100
        assert max(_slices(encoder, _noise(10))) <= 1100

    def test_zero_leaves_slices_unlimited(self) -> None:
        encoder = _encoder()
        encoder._camera.transport_video.slice_max_size = 0
        assert max(_slices(encoder, _noise(10))) > 1100

    def test_a_change_takes_a_new_encoder(self) -> None:
        encoder = _encoder()
        frames = _noise(2)
        _slices(encoder, frames[:1])
        first = encoder._codec
        encoder._camera.transport_video.slice_max_size = 600
        assert max(_slices(encoder, frames[1:])) <= 600
        assert encoder._codec is not first


class TestBitrate:
    def test_movement_after_a_still_scene_keeps_to_the_target(self) -> None:
        """Bits a still scene left unspent are not spent on the next movement."""
        rng = np.random.default_rng(0)
        world = rng.integers(0, 255, (_HEIGHT * 2, _WIDTH * 8, 3), dtype=np.uint8)
        still = [np.ascontiguousarray(world[:_HEIGHT, :_WIDTH])] * (60 * _FPS)
        pan = [
            np.ascontiguousarray(world[:_HEIGHT, x : x + _WIDTH])
            for x in range(0, 3 * _FPS * 20, 20)
        ]
        encoder = _encoder()
        _bits(encoder, still)
        seconds = len(pan) / _FPS
        assert _bits(encoder, pan) / seconds < 1.5 * _TARGET
