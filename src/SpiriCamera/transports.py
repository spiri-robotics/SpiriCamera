"""The ways a camera's frames leave it, each under its own topic.

A transport's settings are ordinary synced fields, at
``<camera>/transport_jpeg/quality``.  Its frames are not: they travel at
``<camera>/transport_jpeg/@data``, and that ``@`` is the point.  A zenoh
key chunk starting with ``@`` is *verbatim*: a wildcard never matches it,
so the ``<camera>/**`` subscription every SpiriSynq mirror holds does not
receive frames, and does not count as wanting them.

That gives a mirror a choice and the camera a signal.  A mirror receives
a transport's frames only by subscribing to that exact key, and a camera
none of whose transports has a subscriber isn't running: its source is
closed.  The transports depend on each other in one direction::

    source -> resize -> widgets -> transport_raw -> transport_jpeg
                                                 -> transport_video

so anything wanting JPEG or video also wants raw, and raw is what keeps
the source open.  A ``spirisynq://`` source subscribes upstream only
while its own camera is running, so demand travels up a chain of relays.

A subscriber joining queries the transport's ``request_keyframe`` key, and
the camera sends its current frame again on ``@data``, as an RTSP viewer's
PLI gets it a keyframe.  A running camera sends new frames anyway, so this
only matters for one whose frame isn't changing, which isn't republished.

This module must not import :py:mod:`SpiriCamera.camera`:
:py:mod:`SpiriCamera.sources` needs these names too.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import zenoh
from psygnal import Signal, SignalGroupDescriptor
from SpiriSynq.qos import SynqQoS
from SpiriSynq.session import Session
from SpiriSynq.syncable_objects import SubSyncableDataclass, WeakMethodProxy

#: QoS of raw and JPEG frames: the lowest priority, below video and well below
#: SpiriSynq's ordinary traffic, and dropped rather than queued.
QOS_BACKGROUND = SynqQoS(
    priority=zenoh.Priority.BACKGROUND, congestion_control=zenoh.CongestionControl.DROP
)

#: QoS of a video interframe: below SpiriSynq's ordinary traffic, and the first of
#: the video to be shed.
QOS_INTERFRAME = SynqQoS(
    priority=zenoh.Priority.DATA_LOW, congestion_control=zenoh.CongestionControl.DROP
)

#: QoS of a video keyframe, the only way back after a loss.  Still dropped under
#: congestion, since a block could stall the encoder on one dead link.
QOS_KEYFRAME = SynqQoS(
    priority=zenoh.Priority.DATA_HIGH, congestion_control=zenoh.CongestionControl.DROP
)

#: Where a camera's raw frames are published, relative to the camera.
TRANSPORT_RAW_DATA = "transport_raw/@data"

#: Where a camera's JPEG frames are published, relative to the camera.
TRANSPORT_JPEG_DATA = "transport_jpeg/@data"

#: Where a camera's video keyframes are published, relative to the camera.
TRANSPORT_VIDEO_KEYFRAME = "transport_video/@data_keyframe"

#: Where the rest of a camera's video is published, relative to the camera.
TRANSPORT_VIDEO_INTERFRAME = "transport_video/@data_interframe"

#: The queryable a video subscriber reports its loss on, relative to the camera.
TRANSPORT_VIDEO_REPORT = "transport_video/report"

#: Seconds between a video subscriber's reports.
REPORT_INTERVAL = 1.0

#: Least seconds between a video subscriber's requests for a keyframe.
KEYFRAME_RETRY = 0.5

#: The queryable a subscriber asks for the current frame on, relative to the
#: transport.  The reply is only an acknowledgement: the frame goes on ``@data``.
REQUEST_KEYFRAME = "request_keyframe"

#: :py:attr:`RawFrame.array`'s pixel format, on the wire and off it.
RAW_FORMAT = "bgr24"


@dataclass(frozen=True, eq=False)
class RawFrame:
    """One captured frame: resized, widgets drawn, not encoded.

    Compares by identity, since pixels are not worth comparing: two
    captures are two frames.

    Attributes
    ----------
    array : np.ndarray
        The BGR pixels, read-only.  Every consumer of a frame shares this
        one array, so one that wants to change it copies it first, as
        widget blending does.
    tags : Mapping[str, str]
        The frame's tags, the same ones a JPEG of it carries in its EXIF.
    """

    array: np.ndarray
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.array.flags.writeable = False

    def to_wire(self) -> tuple[bytes, str]:
        """The frame as a ``(payload, attachment)`` pair for zenoh.

        The payload is the bare pixels and the attachment is JSON saying
        how to read them, which is all a peer in another language needs.
        """
        height, width = self.array.shape[:2]
        attachment = {
            "width": width,
            "height": height,
            "format": RAW_FORMAT,
            "tags": dict(self.tags),
        }
        return self.array.tobytes(), json.dumps(attachment)

    @classmethod
    def from_wire(cls, payload: bytes, attachment: str) -> RawFrame:
        """Rebuild a frame from :py:meth:`to_wire`'s pair.

        Raises
        ------
        ValueError
            If the attachment doesn't describe the payload.
        """
        info = json.loads(attachment)
        if info["format"] != RAW_FORMAT:
            raise ValueError(f"raw frame is {info['format']!r}, not {RAW_FORMAT!r}")
        shape = (int(info["height"]), int(info["width"]), 3)
        array = np.frombuffer(payload, dtype=np.uint8).reshape(shape)
        return cls(array, dict(info.get("tags", {})))


@dataclass(frozen=True, eq=False)
class VideoPacket:
    """One encoded frame of a camera's video.

    Compares by identity, as a :py:class:`RawFrame` does.

    Attributes
    ----------
    payload : bytes
        The encoded frame, opaque: Annex B H.264, and a keyframe carries its
        SPS and PPS, so it can be decoded with no prior state.
    keyframe : bool
        Whether this is a keyframe, which is which key it travels on.
    sequence : int
        Counts up by one per packet, across both keys.
    keyframe_sequence : int
        The :py:attr:`sequence` of the keyframe this packet's chain starts at.
    pts : int
        Presentation time, at a 90 kHz clock.
    width : int
        Encoded width in pixels.
    height : int
        Encoded height in pixels.
    codec : str
        What :py:attr:`payload` is encoded as.
    tags : Mapping[str, str]
        The frame's tags, as on its :py:class:`RawFrame`.
    """

    payload: bytes
    keyframe: bool
    sequence: int
    keyframe_sequence: int
    pts: int
    width: int
    height: int
    codec: str = "h264"
    tags: Mapping[str, str] = field(default_factory=dict)

    def attachment(self) -> str:
        """Everything but the payload, as JSON, for a zenoh attachment."""
        return json.dumps(
            {
                "sequence": self.sequence,
                "keyframe_sequence": self.keyframe_sequence,
                "pts": self.pts,
                "width": self.width,
                "height": self.height,
                "codec": self.codec,
                "tags": dict(self.tags),
            }
        )

    @classmethod
    def from_wire(cls, payload: bytes, attachment: str, *, keyframe: bool) -> VideoPacket:
        """Rebuild a packet from its payload and :py:meth:`attachment`.

        Raises
        ------
        ValueError, KeyError
            If the attachment isn't one.
        """
        info = json.loads(attachment)
        return cls(
            payload=payload,
            keyframe=keyframe,
            sequence=int(info["sequence"]),
            keyframe_sequence=int(info["keyframe_sequence"]),
            pts=int(info["pts"]),
            width=int(info["width"]),
            height=int(info["height"]),
            codec=str(info["codec"]),
            tags=dict(info.get("tags", {})),
        )


class VideoSync:
    """What a video subscriber needs to know which packets it can decode.

    The job an RTP stack would otherwise do.  An interframe is only
    any use if everything back to its keyframe arrived, and the two keys can
    be at different priorities, so a keyframe can overtake the interframes
    before it:

    * Until a keyframe arrives, nothing else can be decoded.
    * An interframe from before the current keyframe is discarded.
    * An interframe that isn't next in sequence means a gap: stop, and ask
      for a keyframe.

    Parameters
    ----------
    keyframes_only : bool
        Whether this subscriber only takes keyframes, so the sequence numbers
        between them are not missing, just not for it.
    """

    def __init__(self, *, keyframes_only: bool = False) -> None:
        self.keyframes_only = keyframes_only
        self._synced = False
        self._last = 0
        self._keyframe_sequence = 0
        self._received = 0
        self._lost = 0
        self._last_request = 0.0
        self._last_report = time.monotonic()

    def accept(self, packet: VideoPacket) -> bool:
        """Whether ``packet`` can be decoded next, updating what's known."""
        if packet.keyframe:
            if self._synced and packet.sequence <= self._last:
                return False
            if self._last and not self.keyframes_only:
                self._lost += max(0, packet.sequence - self._last - 1)
            self._received += 1
            self._synced = True
            self._last = packet.sequence
            self._keyframe_sequence = packet.sequence
            return True

        if not self._synced or packet.keyframe_sequence < self._keyframe_sequence:
            return False
        if packet.sequence <= self._last:
            return False
        if packet.keyframe_sequence != self._keyframe_sequence or (
            packet.sequence != self._last + 1
        ):
            self._lost += packet.sequence - self._last - 1
            self._last = packet.sequence
            self._synced = False
            return False
        self._received += 1
        self._last = packet.sequence
        return True

    def keyframe_due(self) -> bool:
        """Whether to ask for a keyframe now, at most every :py:data:`KEYFRAME_RETRY`."""
        now = time.monotonic()
        if self._synced or now - self._last_request < KEYFRAME_RETRY:
            return False
        self._last_request = now
        return True

    def take_report(self) -> float | None:
        """The fraction lost since the last report, once per :py:data:`REPORT_INTERVAL`.

        Never for a keyframes-only subscriber: with no sequence to check it
        can't see loss, and shouldn't tell rate control the link is clean.
        """
        if self.keyframes_only:
            return None
        now = time.monotonic()
        if now - self._last_report < REPORT_INTERVAL:
            return None
        self._last_report = now
        expected = self._received + self._lost
        lost = self._lost / expected if expected else 0.0
        self._received = self._lost = 0
        return lost


class _Transport(SubSyncableDataclass):
    """What every transport has: the current frame, and a signal for a new one.

    :py:attr:`data` is deliberately not a dataclass field.  A field would be
    a psygnal signal relayed through the camera's whole event tree, once per
    frame, only to be filtered out before publishing.  That filter has also
    been seen to miss: a frame reached SpiriSynq's field publishing under
    the bare path ``transport_raw``, and failed to serialize.
    """

    data_changed: ClassVar[Signal] = Signal(object)
    #: Local to the sender, as SpiriSynq's ``<field>_qos`` is; set it on the
    #: class or the instance.
    data_qos: ClassVar[SynqQoS] = QOS_BACKGROUND

    @property
    def data(self) -> object:
        """The most recent frame; see the subclasses for what that is."""
        return getattr(self, "_data", self._empty_data)

    @data.setter
    def data(self, value: object) -> None:
        if value != self.data:
            self._data = value
            self.data_changed.emit(value)

    def forget(self) -> None:
        """Drop the current frame without announcing it: it isn't news."""
        self._data = self._empty_data


@dataclass
class TransportRaw(_Transport):
    """A camera's raw transport, which every other transport is built on.

    Attributes
    ----------
    data : RawFrame | None
        The most recent frame, or ``None`` while nothing wants frames.
        Published at :py:data:`TRANSPORT_RAW_DATA` rather than synced as a
        field, so it is not part of a rehydrate either.
    data_changed : Signal
        Emits each new :py:attr:`data`, wherever it came from.
    """

    # Nothing to signal yet: raw has no settings, only data_changed.
    events: ClassVar[SignalGroupDescriptor] = SignalGroupDescriptor(warn_on_no_fields=False)

    _empty_data: ClassVar[RawFrame | None] = None


@dataclass
class TransportJpeg(_Transport):
    """A camera's JPEG transport.

    Attributes
    ----------
    quality : int
        Encode quality, 1-100.  Live: takes effect on the next frame.
    mimetype : str
        What :py:attr:`data` is encoded as; see
        :py:data:`SpiriCamera.camera.ENCODINGS`.
    data : bytes
        The most recent encoded frame, carrying that frame's tags in its
        EXIF.  Empty until someone wants frames.  Published at
        :py:data:`TRANSPORT_JPEG_DATA` rather than synced as a field, so it
        is not part of a rehydrate either.
    data_changed : Signal
        Emits each new :py:attr:`data`, wherever it came from.
    """

    _empty_data: ClassVar[bytes] = b""

    quality: int = 80
    mimetype: str = "image/jpeg"


@dataclass
class RateControl(SubSyncableDataclass):
    """How the one encode suits itself to the worst subscriber's link.

    All policy, and all live: see :py:class:`SpiriCamera.video.RateController`.

    Attributes
    ----------
    start_bitrate : int
        A new subscriber's estimate, and the target with no reports, in bps.
    min_bitrate : int
        The encoder target's floor, in bps.  Below it, frame rate drops instead.
    max_bitrate : int
        The encoder target's ceiling, in bps.
    min_fps : float
        The lowest frame rate to drop to.  The highest is the camera's
        ``max_framerate``.
    loss_high : float
        Reported loss above which the estimate is cut.
    loss_low : float
        Reported loss below which the estimate grows.
    growth : float
        What a clean report multiplies the estimate by.
    stall_timeout : float
        Seconds without a report before the estimate is halved, repeatedly.
    subscriber_timeout : float
        Seconds without a report before a subscriber stops counting at all.
    height_caps : list
        ``[minimum target, frame height cap or null]`` pairs, first match wins:
        a lower target is spent on fewer, cleaner pixels.  Empty never shrinks.
    bitrate_restart_fraction : float
        How far the target must move from the running encoder's bitrate to be
        worth applying.  Applying one takes a new encoder, so a keyframe.
    bitrate_restart_interval : float
        Seconds without a keyframe before a change in target gets one of its
        own.  Until then it waits for the next keyframe sent for any other
        reason, so adapting costs no extra keyframes.
    """

    start_bitrate: int = 1_000_000
    min_bitrate: int = 500_000
    max_bitrate: int = 3_000_000
    # float | int: SpiriSynq refuses an int for a plain float field, and someone
    # setting these shouldn't have their "2" ignored for not being "2.0".
    min_fps: float | int = 2.0
    loss_high: float | int = 0.10
    loss_low: float | int = 0.02
    growth: float | int = 1.08
    stall_timeout: float | int = 3.0
    subscriber_timeout: float | int = 10.0
    bitrate_restart_fraction: float | int = 0.1
    bitrate_restart_interval: float | int = 10.0
    height_caps: list = field(default_factory=lambda: [[1_500_000, None], [800_000, 720], [0, 480]])


@dataclass
class TransportVideo(SubSyncableDataclass):
    """A camera's video transport: its one encode, whoever is watching.

    Keyframes and interframes travel on separate keys so each can have its
    own QoS: a keyframe is the only way back after a loss, and the frames
    between are the first thing to shed.  The QoS is SpiriSynq's own kind:
    local to the sender, never synced or rehydrated, and set in code on the
    class or the instance as ``data_keyframe_qos`` and ``data_interframe_qos``.

    Attributes
    ----------
    codec : str
        What is encoded.  Only ``h264``.
    preset : str
        libx264's preset; defaults to ``SPIRICAMERA_H264_PRESET``.
    keyframe_interval : float
        Seconds between keyframes, which bounds recovery from a loss.  Also
        the frame interval of a keyframes-only subscriber.  0 sends them only
        when asked for.
    keyframe_min_interval : float
        Least seconds between requested keyframes, so ten joiners or one
        lossy link don't cause a storm.
    target_bitrate : int
        What the encoder is aiming at now, for the worst subscriber.  A
        readout, set by the encoder: anything else written to it is overwritten.
    encoded_fps : float
        The frame rate being encoded now, also a readout: the camera's
        ``max_framerate``, lower only when rate control says so.
    encoded_width : int
        The width being encoded now, also a readout.
    encoded_height : int
        The height being encoded now, also a readout: the camera's
        ``max_height`` is the setting, and this is lower only when
        ``rate_control.height_caps`` says so.
    rate_control : RateControl
        The policy behind those readouts.
    packet_received : Signal
        Emits each :py:class:`VideoPacket` in decodable order, in this process,
        which is what RTSP and other endpoints read.
    """

    packet_received: ClassVar[Signal] = Signal(object)
    data_keyframe_qos: ClassVar[SynqQoS] = QOS_KEYFRAME
    data_interframe_qos: ClassVar[SynqQoS] = QOS_INTERFRAME

    codec: str = "h264"
    preset: str = "veryfast"
    keyframe_interval: float | int = 2.0
    keyframe_min_interval: float | int = 0.25
    target_bitrate: int = 0
    encoded_fps: float | int = 0.0
    encoded_width: int = 0
    encoded_height: int = 0
    rate_control: RateControl = field(default_factory=RateControl)


def _ignore_reply(reply: zenoh.Reply) -> None:
    """A request's acknowledgement: what matters arrives on the ``@data`` keys."""


class DataChannel:
    """The zenoh side of one ``@data`` key, and who wants it.

    A camera wants a transport's frames for two kinds of consumer: a
    subscriber somewhere on the network, which the key's publisher can
    see, and something in this process, which *holds* the channel instead
    so its frames are not sent through zenoh just to come back.

    Parameters
    ----------
    path : str
        The key, relative to the camera.
    receive : Callable[[zenoh.Sample], None]
        Bound method called with each frame from the network.
    answers_keyframe_requests : bool
        Whether this channel owns its transport's ``request_keyframe``
        queryable.  A transport has one, however many keys it has.
    """

    def __init__(
        self,
        path: str,
        receive: Callable[[zenoh.Sample], None],
        *,
        answers_keyframe_requests: bool = True,
    ) -> None:
        self.path = path
        self.key = ""
        # Weak, like SpiriSynq's own callbacks: the channel must not keep
        # its camera alive through a zenoh callback.
        self._receive = WeakMethodProxy(receive)
        self._answers_keyframe_requests = answers_keyframe_requests
        self._session: Session | None = None
        self._authoritive = False
        self._publisher: zenoh.Publisher | None = None
        self._request_queryable: zenoh.Queryable | None = None
        self._subscriber: zenoh.Subscriber | None = None
        self._transport_key = ""
        self._lock = threading.Lock()
        self._holds = 0
        self._subscribed = False
        self._keyframe_requested = False

    def declare(self, session: Session, camera_path: str, *, authoritive: bool) -> None:
        """Go live under ``camera_path``."""
        self._session = session
        self._authoritive = authoritive
        self.key = f"{camera_path}/{self.path}"
        self._transport_key = self.key.rsplit("/", 1)[0]
        self._publisher = session.zenoh_session.declare_publisher(self.key)
        if authoritive and self._answers_keyframe_requests:
            self._request_queryable = session.zenoh_session.declare_queryable(
                f"{self._transport_key}/{REQUEST_KEYFRAME}",
                WeakMethodProxy(self._on_keyframe_request),
            )

    @property
    def demanded(self) -> bool:
        """Whether a subscriber on the network, or a hold here, wants frames."""
        return self._holds > 0 or self.remote_wanted

    @property
    def wanted(self) -> bool:
        """Whether a frame should be produced for this channel's ``data``.

        As :py:attr:`demanded`, and also before the channel has been declared:
        a camera not on SpiriSynq has nobody to ask, and only local readers.
        """
        return self._publisher is None or self.demanded

    @property
    def remote_wanted(self) -> bool:
        """Whether a subscriber on the network would receive a frame sent now."""
        return self._publisher is not None and self._publisher.matching_status.matching

    @property
    def subscribed(self) -> bool:
        """Whether :py:meth:`subscribe` is in effect here."""
        return self._subscribed

    def hold(self) -> None:
        """Want this channel's frames for a consumer in this process."""
        with self._lock:
            self._holds += 1

    def release(self) -> None:
        """Undo one :py:meth:`hold`."""
        with self._lock:
            self._holds = max(0, self._holds - 1)

    def subscribe(self) -> None:
        """Receive frames here; subscribing again is a no-op.

        The camera itself already has its own frames, so this only holds
        the channel.  A mirror declares a subscriber, and asks for a keyframe
        in case nothing new is coming.
        """
        with self._lock:
            if self._subscribed:
                return
            self._subscribed = True
            if self._authoritive or self._session is None:
                self._holds += 1
                return
            self._subscriber = self._session.zenoh_session.declare_subscriber(
                self.key, WeakMethodProxy(self._on_sample)
            )
        if self._answers_keyframe_requests:
            self.request_keyframe()

    def unsubscribe(self) -> bool:
        """Stop receiving frames here.

        Returns
        -------
        bool
            Whether this undid a :py:meth:`subscribe`.
        """
        with self._lock:
            if not self._subscribed:
                return False
            self._subscribed = False
            subscriber, self._subscriber = self._subscriber, None
            if subscriber is None:
                self._holds = max(0, self._holds - 1)
        if subscriber is not None:
            subscriber.undeclare()
        return True

    def request_keyframe(self) -> None:
        """Ask the camera to send a keyframe on this transport."""
        self.query(REQUEST_KEYFRAME)

    def query(self, name: str, payload: str | None = None) -> None:
        """Query one of this transport's queryables, not waiting for the reply."""
        if self._session is not None:
            self._session.zenoh_session.get(
                f"{self._transport_key}/{name}", _ignore_reply, payload=payload
            )

    def take_keyframe_request(self) -> bool:
        """Whether a subscriber has asked for a keyframe since this was last called."""
        requested, self._keyframe_requested = self._keyframe_requested, False
        return requested

    def put(self, payload: bytes, **options: object) -> None:
        """Publish a frame that started here, if anyone would receive it.

        Through the session rather than the publisher, as SpiriSynq's own
        field puts are: only a session put can set QoS per sample, which is
        what lets it change live.  The publisher's id still names the sender.
        """
        if self._session is None or not self.remote_wanted:
            return
        publisher = self._publisher
        assert publisher is not None
        self._session.zenoh_session.put(
            self.key,
            payload,
            source_info=self._session.source_info(source_id=publisher.id),
            **options,  # type: ignore[arg-type]
        )

    def close(self) -> None:
        """Undeclare everything this channel declared."""
        for name in ("_subscriber", "_request_queryable", "_publisher"):
            entity = getattr(self, name)
            setattr(self, name, None)
            if entity is not None:
                entity.undeclare()
        self._session = None

    def _on_sample(self, sample: zenoh.Sample) -> None:
        """A live frame; our own come back too, and are dropped."""
        publisher = self._publisher
        source = sample.source_info.source_id if sample.source_info else None
        if (
            publisher is not None
            and source is not None
            and (str(source.zid), source.eid) == (str(publisher.id.zid), publisher.id.eid)
        ):
            return
        if sample.kind == zenoh.SampleKind.PUT and sample.payload:
            self._receive(sample)

    def _on_keyframe_request(self, query: zenoh.Query) -> None:
        """A subscriber just joined; the camera acts on it at its next frame."""
        self._keyframe_requested = True
        query.reply(query.key_expr, b"")
