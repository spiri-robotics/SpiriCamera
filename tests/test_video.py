"""The H.264 encode in :py:mod:`SpiriCamera.video`."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from SpiriCamera.rtsp import split_annexb
from SpiriCamera.transports import TransportVideo
from SpiriCamera.video import VideoEncoder

_FPS = 30
_TARGET = 300_000
_WIDTH, _HEIGHT = 320, 180


def _encoder(*, intra_refresh: bool = False) -> VideoEncoder:
    transport = TransportVideo(intra_refresh=intra_refresh)
    camera = SimpleNamespace(transport_video=transport, max_framerate=_FPS)
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


def _pan(count: int, step: int = 8) -> list[np.ndarray]:
    rng = np.random.default_rng(1)
    world = rng.integers(0, 255, (_HEIGHT, _WIDTH + count * step, 3), dtype=np.uint8)
    return [np.ascontiguousarray(world[:, x : x + _WIDTH]) for x in range(0, count * step, step)]


def _packets(
    encoder: VideoEncoder, frames: list[np.ndarray], keyframes: set[int] = frozenset({0})
) -> list[tuple[int, bool]]:
    """``(size, is_keyframe)`` for each frame, forcing keyframes where asked."""
    out = []
    for index, frame in enumerate(frames):
        for payload, is_keyframe, *_ in encoder._encode(
            frame, _TARGET, None, index in keyframes, 0.0
        ):
            out.append((len(payload), is_keyframe))
    return out


class TestIntraRefresh:
    def test_off_by_default(self) -> None:
        assert not TransportVideo().intra_refresh

    def test_no_keyframe_but_the_first(self) -> None:
        packets = _packets(_encoder(intra_refresh=True), _pan(4 * _FPS))
        assert [index for index, (_, key) in enumerate(packets) if key] == [0]

    def test_spreads_the_keyframe_over_the_sweep(self) -> None:
        """No frame after the first is a keyframe's size."""
        sizes = [size for size, _ in _packets(_encoder(intra_refresh=True), _pan(4 * _FPS))]
        assert max(sizes[1:]) < sizes[0] / 2

    def test_a_requested_keyframe_is_a_keyframe(self) -> None:
        packets = _packets(_encoder(intra_refresh=True), _pan(3 * _FPS), keyframes={0, 50})
        assert [index for index, (_, key) in enumerate(packets) if key] == [0, 50]

    def test_off_marks_only_idr_frames_as_keyframes(self) -> None:
        packets = _packets(_encoder(), _pan(3 * _FPS), keyframes={0, 60})
        assert [index for index, (_, key) in enumerate(packets) if key] == [0, 60]


class _Channel:
    def __init__(self, demanded: bool) -> None:
        self.demanded = demanded

    def take_keyframe_request(self) -> bool:
        return False


def _driven(interframes: bool, intra_refresh: bool) -> tuple[VideoEncoder, list[bool]]:
    """An encoder whose _encode_next sees a new frame each call, and what it sent."""
    sent: list[bool] = []
    raw = SimpleNamespace(data=None)
    camera = SimpleNamespace(
        transport_video=TransportVideo(intra_refresh=intra_refresh),
        transport_raw=raw,
        max_framerate=_FPS,
        _video_keyframe=_Channel(True),
        _video_interframes_demanded=lambda: interframes,
        _publish_video=lambda packet: sent.append(packet.keyframe),
    )
    return VideoEncoder(camera), sent  # type: ignore[arg-type]


def _drive(encoder: VideoEncoder, seconds: float, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("SpiriCamera.video.time.monotonic", lambda: clock[0])
    for frame in _pan(int(seconds * _FPS)):
        encoder._camera.transport_raw.data = SimpleNamespace(array=frame, tags={})
        encoder._encode_next(_TARGET, None, 0)
        clock[0] += 1 / _FPS


class TestPeriodicKeyframes:
    def test_none_with_intra_refresh_and_viewers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        encoder, sent = _driven(interframes=True, intra_refresh=True)
        _drive(encoder, 5, monkeypatch)
        assert sent.count(True) == 1

    def test_every_interval_without_intra_refresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        encoder, sent = _driven(interframes=True, intra_refresh=False)
        _drive(encoder, 5, monkeypatch)
        assert sent.count(True) == 3  # 0, 2 and 4 seconds

    def test_every_interval_for_keyframes_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        encoder, sent = _driven(interframes=False, intra_refresh=True)
        _drive(encoder, 5, monkeypatch)
        assert sent == [True, True, True]


class TestRetarget:
    def test_a_lower_target_keeps_the_encoder(self) -> None:
        encoder = _encoder()
        frames = _pan(2)
        encoder._encode(frames[0], _TARGET, None, True, 0.0)
        first = encoder._codec
        encoder._encode(frames[1], int(_TARGET * 0.7), None, False, 0.0)
        assert encoder._codec is first
        assert first.bit_rate == int(_TARGET * 0.7)

    def test_a_higher_target_waits_for_a_restart(self) -> None:
        encoder = _encoder()
        frames = _pan(3)
        encoder._encode(frames[0], _TARGET, None, True, 0.0)
        first = encoder._codec
        encoder._encode(frames[1], _TARGET * 2, None, False, 0.0)
        assert encoder._codec is first
        assert first.bit_rate == _TARGET
        encoder._encode(frames[2], _TARGET * 2, None, False, 60.0)
        assert encoder._codec is not first

    def test_far_under_the_ceiling_restarts(self) -> None:
        encoder = _encoder()
        frames = _pan(2)
        encoder._encode(frames[0], _TARGET, None, True, 0.0)
        first = encoder._codec
        encoder._encode(frames[1], _TARGET // 3, None, True, 0.0)
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
