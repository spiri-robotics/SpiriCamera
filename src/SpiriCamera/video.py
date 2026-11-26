"""A camera's one H.264 encode, and the rate control that suits it to its worst link.

Everything that wants a camera as video reads this one encode, through
:py:attr:`TransportVideo.packet_received
<SpiriCamera.transports.TransportVideo.packet_received>` in this process or
``transport_video/@data_*`` over SpiriSynq, so however many there are, each
frame is encoded once.  It suits the worst of them: the lowest target
bitrate, frame rate and height cap among the subscribers' reports.
"""

from __future__ import annotations

import fractions
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import av
import numpy as np
from av.video.frame import PictureType, VideoFrame
from loguru import logger

from SpiriCamera.sources.base import shrink
from SpiriCamera.transports import RateControl, RawFrame, VideoPacket

if TYPE_CHECKING:
    from SpiriCamera.camera import Camera


def _positive(value: object) -> int:
    """A bound UI or remote value as a positive integer."""
    try:
        return max(1, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 1


#: RTP's clock for video, which :py:attr:`VideoPacket.pts` counts in.
VIDEO_CLOCK_RATE = 90_000

#: Seconds between checks for a subscriber, while nothing wants video.
_IDLE_POLL_INTERVAL = 0.1

#: Seconds of the target bitrate the encoder may run ahead by, as x264's VBV buffer:
#: room for a keyframe, and the most a burst can put on the link at once.
_VBV_BUFFER_SECONDS = 1.0

#: A target this far under the running encoder's VBV ceiling gets a new encoder.
_VBV_RETIGHTEN = 0.5

#: H.264's NAL unit type for a slice of an IDR picture.
_NAL_IDR = 5


def _has_idr(payload: bytes) -> bool:
    """Whether an Annex B access unit holds an IDR slice, so decodes on its own."""
    start = payload.find(b"\x00\x00\x01")
    while 0 <= start < len(payload) - 3:
        if payload[start + 3] & 0x1F == _NAL_IDR:
            return True
        start = payload.find(b"\x00\x00\x01", start + 3)
    return False


class RateController:
    """One subscriber's estimate of what its link will carry.

    Every subscriber reports the fraction of frames it lost, about once a
    second, and each report moves the estimate: cut on heavy loss, grown on
    clean reports, halved if reports stop.  A ``max_bitrate`` the subscriber
    reports itself is kept as a ceiling.

    The target stays between ``min_bitrate`` and ``max_bitrate``.  Below the
    floor, the estimate is instead met by lowering :py:attr:`fps`, and the
    frame is capped to :py:attr:`max_height` to suit the target.

    Parameters
    ----------
    settings : RateControl
        The policy, read afresh every time so changes to it are live.
    max_fps : Callable[[], float]
        The frame rate while the link has room, which is the camera's.
    """

    def __init__(self, settings: RateControl, max_fps: Callable[[], float]) -> None:
        self.settings = settings
        self._max_fps = max_fps
        self.max_bitrate: int | None = None
        self.estimate = float(settings.start_bitrate)
        self.last_report = time.monotonic()
        self.last_seen = self.last_report

    @property
    def target(self) -> int:
        """Encoder target: the estimate, clamped to the floor and ceiling."""
        settings = self.settings
        return int(max(settings.min_bitrate, min(self.estimate, settings.max_bitrate)))

    @property
    def fps(self) -> float:
        """Frame rate that fits the estimate into the target."""
        return self._max_fps() * min(1.0, self.estimate / self.target)

    @property
    def max_height(self) -> int | None:
        """Frame height cap for the target, or ``None`` for no cap."""
        target = self.target
        for minimum, cap in self.settings.height_caps:
            if target >= minimum:
                return cap
        return None

    def report(self, fraction_lost: float | None, max_bitrate: int | None) -> None:
        """Fold one report into the estimate."""
        self.last_report = self.last_seen = time.monotonic()
        settings = self.settings
        if max_bitrate is not None:
            self.max_bitrate = int(max_bitrate)
        if fraction_lost is not None:
            if fraction_lost > settings.loss_high:
                self.estimate *= 1 - 0.5 * fraction_lost
            elif fraction_lost < settings.loss_low:
                self.estimate *= settings.growth
        self._clamp()

    def stall(self) -> None:
        """Halve the estimate: reports have stopped, so the link is in trouble."""
        self.last_report = time.monotonic()
        self.estimate /= 2
        self._clamp()

    def _clamp(self) -> None:
        settings = self.settings
        upper = settings.max_bitrate
        if self.max_bitrate is not None:
            upper = min(upper, self.max_bitrate)
        lower = settings.min_bitrate * min(settings.min_fps / self._max_fps(), 1.0)
        self.estimate = max(lower, min(self.estimate, upper))


class VideoEncoder:
    """Encodes a camera's raw frames while anything wants them as video.

    Runs on its own thread, so a slow x264 holds up neither capture nor
    JPEG.  It always takes the newest raw frame, skipping any it was too
    slow for.

    Parameters
    ----------
    camera : Camera
        The camera whose frames to encode.
    """

    def __init__(self, camera: Camera) -> None:
        self._camera = camera
        self._lock = threading.Lock()
        self._rates: dict[str, RateController] = {}
        self._keyframe_requested = True
        self._codec: av.CodecContext | None = None
        self._codec_x264: tuple[str, int, int] = ("", 0, 0)
        self._codec_maxrate = 0
        self._frames = 0
        self._sequence = 0
        self._keyframe_sequence = 0
        self._last_keyframe = 0.0
        self._last_raw: RawFrame | None = None
        self._complaint = ""

    def request_keyframe(self) -> None:
        """Make the next frame a keyframe, as soon as ``keyframe_min_interval`` allows."""
        self._keyframe_requested = True

    def report(self, subscriber: str, fraction_lost: float | None, max_bitrate: int | None) -> None:
        """Take a subscriber's report, which is what enters it into rate control."""
        with self._lock:
            rate = self._rates.get(subscriber)
            if rate is None:
                settings = self._camera.transport_video.rate_control
                rate = self._rates[subscriber] = RateController(settings, self._max_fps)
            rate.report(fraction_lost, max_bitrate)

    def _max_fps(self) -> float:
        """The frame rate with room to spare: the camera's, the one setting for it."""
        return float(max(1, _positive(self._camera.max_framerate)))

    def forget(self, subscriber: str) -> None:
        """Take a subscriber that has left out of rate control."""
        with self._lock:
            self._rates.pop(subscriber, None)

    def run(self, stop: threading.Event) -> None:
        """Encode until ``stop`` is set; the body of the encoder's thread."""
        # A restart starts from a keyframe, as a wake-up does.
        self._go_idle()
        started = due = time.monotonic()
        while not stop.is_set():
            if not self._camera._video_demanded():
                self._go_idle()
                stop.wait(_IDLE_POLL_INTERVAL)
                started = due = time.monotonic()
                continue
            try:
                target, fps, cap = self._worst()
                due += 1 / fps
                now = time.monotonic()
                if due > now:
                    stop.wait(due - now)
                else:
                    # Fell behind; carry on from now rather than bursting.
                    due = now
                self._encode_next(target, cap, int((due - started) * VIDEO_CLOCK_RATE))
                self._complaint = ""
            except Exception as exc:
                # A bad setting, say. Live settings can be put right, so keep going.
                if str(exc) != self._complaint:
                    self._complaint = str(exc)
                    logger.warning(f"{self._camera.synq_topic}: video not encoding, {exc}")
                self._codec = None
                stop.wait(_IDLE_POLL_INTERVAL)

    def _go_idle(self) -> None:
        """Nothing wants video: drop the encoder and start from a keyframe next time."""
        self._codec = None
        self._last_raw = None
        self._keyframe_requested = True
        with self._lock:
            self._rates.clear()

    def _worst(self) -> tuple[int, float, int | None]:
        """The encode that suits every subscriber: ``(target, fps, height cap)``."""
        transport = self._camera.transport_video
        settings = transport.rate_control
        now = time.monotonic()
        with self._lock:
            for subscriber, rate in list(self._rates.items()):
                if now - rate.last_seen > settings.subscriber_timeout:
                    del self._rates[subscriber]
                elif now - rate.last_report > settings.stall_timeout:
                    rate.stall()
            rates = list(self._rates.values())
        if rates:
            target = min(rate.target for rate in rates)
            fps = max(
                min(rate.fps for rate in rates), min(float(settings.min_fps), self._max_fps())
            )
            caps = [rate.max_height for rate in rates if rate.max_height is not None]
            cap = min(caps, default=None)
        else:
            # Nobody has said anything about their link yet.
            target, fps, cap = int(settings.start_bitrate), self._max_fps(), None
        transport.target_bitrate = target
        transport.encoded_fps = round(fps, 2)
        return target, fps, cap

    def _encode_next(self, target: int, cap: int | None, pts: int) -> None:
        """Encode and publish the next frame, if there's a reason to."""
        camera = self._camera
        transport = camera.transport_video
        raw = camera.transport_raw.data
        if raw is None:
            return

        now = time.monotonic()
        if camera._video_keyframe.take_keyframe_request():
            self._keyframe_requested = True
        since_keyframe = now - self._last_keyframe
        interframes = camera._video_interframes_demanded()
        # With intra-refresh, x264 repairs the picture a band at a time, and the
        # only keyframes are asked for: a joiner's, a PLI's, a restart's.  With
        # only keyframes-only subscribers, keyframes are all there is to send.
        periodic = (not transport.intra_refresh or not interframes) and (
            0 < float(transport.keyframe_interval) <= since_keyframe
        )
        keyframe = (
            self._keyframe_requested and since_keyframe >= float(transport.keyframe_min_interval)
        ) or periodic
        changed = raw is not self._last_raw
        # With only keyframe subscribers, the frames between are for nobody.
        if not keyframe and not (changed and interframes):
            return

        self._last_raw = raw
        encoded = self._encode(raw.array, target, cap, keyframe, since_keyframe)
        for payload, is_keyframe, width, height in encoded:
            self._sequence += 1
            if is_keyframe:
                self._keyframe_sequence = self._sequence
                self._last_keyframe = now
                self._keyframe_requested = False
            camera._publish_video(
                VideoPacket(
                    payload=payload,
                    keyframe=is_keyframe,
                    sequence=self._sequence,
                    keyframe_sequence=self._keyframe_sequence,
                    pts=pts,
                    width=width,
                    height=height,
                    codec=transport.codec,
                    tags=raw.tags,
                )
            )

    def _encode(
        self,
        array: np.ndarray,
        target: int,
        cap: int | None,
        keyframe: bool,
        since_keyframe: float,
    ) -> list[tuple[bytes, bool, int, int]]:
        """One frame's packets, as ``(payload, is_keyframe, width, height)``."""
        transport = self._camera.transport_video
        if transport.codec != "h264":
            raise ValueError(f"cannot encode {transport.codec!r}, supported: h264")
        if cap is not None and array.shape[0] > cap:
            array = shrink(array, (round(array.shape[1] * cap / array.shape[0]), cap))
        # yuv420p needs even sides.
        height, width = array.shape[0] // 2 * 2, array.shape[1] // 2 * 2
        array = array[:height, :width]
        transport.encoded_width, transport.encoded_height = width, height

        codec = self._codec
        max_fps = max(1, int(self._max_fps()))
        settings = transport.rate_control
        fraction = float(settings.bitrate_restart_fraction)
        refresh_frames = round(float(transport.keyframe_interval) * max_fps)
        x264 = (
            str(transport.preset),
            max(0, int(transport.slice_max_size)),
            bool(transport.intra_refresh) and refresh_frames,
        )
        maxrate = self._codec_maxrate
        # Retargeting a running encoder can't raise its VBV ceiling, and a target
        # far under it leaves bursts the link no longer carries: both take a new
        # encoder, when a keyframe would be due anyway.
        beyond_vbv = target > maxrate * (1 + fraction) or target < maxrate * _VBV_RETIGHTEN
        if (
            codec is None
            or (codec.width, codec.height) != (width, height)
            or (
                beyond_vbv
                and (keyframe or since_keyframe >= float(settings.bitrate_restart_interval))
            )
            or self._codec_x264 != x264
            or codec.framerate != fractions.Fraction(max_fps, 1)
        ):
            # A new encoder, for a new size or a changed bitrate; its first
            # frame is a keyframe.
            codec = self._codec = av.CodecContext.create("libx264", "w")
            codec.width, codec.height = width, height
            codec.bit_rate = target
            codec.pix_fmt = "yuv420p"
            # What libx264 is told, whatever arrives: it spends target / max_fps
            # per frame, so 10 fps at a 500 kbps target sends ~160 kbps.  That
            # is how a link gets below the bitrate floor.
            codec.framerate = fractions.Fraction(max_fps, 1)
            codec.time_base = fractions.Fraction(1, max_fps)
            # A preset, because x264's default (medium) is far too slow for
            # live video on a Pi.  maxrate and bufsize put x264 under VBV: without
            # them its ABR averages over the encoder's whole life, so bits a still
            # scene left unspent are spent on the next movement, at 5x the target
            # after a minute still and 12x after six.
            preset, slice_max_size, intra_refresh = x264
            options = {
                "level": "31",
                "tune": "zerolatency",
                "preset": preset,
                "maxrate": str(target),
                "bufsize": str(int(target * _VBV_BUFFER_SECONDS)),
            }
            params = []
            if slice_max_size:
                # Slices small enough for one RTP packet each, so a lost packet
                # costs a strip of one frame rather than the whole frame.
                params.append(f"slice-max-size={slice_max_size}")
            if intra_refresh:
                # A band of intra blocks sweeps the picture every keyframe_interval
                # (in frames at max_fps), instead of a keyframe: loss heals within
                # one sweep, and there's no keyframe to burst onto the link.
                params += ["intra-refresh=1", f"keyint={intra_refresh}"]
            if params:
                options["x264-params"] = ":".join(params)
            codec.options = options
            codec.profile = "Baseline"
            self._codec_x264 = x264
            self._codec_maxrate = target
        elif abs(target - codec.bit_rate) / codec.bit_rate > fraction and not beyond_vbv:
            # Within the VBV ceiling, a running encoder takes a new target as is:
            # no new encoder, so no keyframe.
            codec.bit_rate = min(target, self._codec_maxrate)

        frame = VideoFrame.from_ndarray(array, format="bgr24")
        # Counted, not timed: see the framerate above.
        frame.pts = self._frames
        self._frames += 1
        frame.pict_type = PictureType.I if keyframe else PictureType.NONE
        # libav flags the start of each intra-refresh sweep as a keyframe too, but
        # only an IDR decodes on its own, which is what a keyframe promises.
        packets = [bytes(packet) for packet in codec.encode(frame)]
        return [(payload, _has_idr(payload), width, height) for payload in packets]


__all__ = ["VIDEO_CLOCK_RATE", "RateController", "VideoEncoder"]
