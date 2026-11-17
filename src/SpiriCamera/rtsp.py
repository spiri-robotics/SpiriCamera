"""The cameras' RTSP server: each one's H.264 encode and KLV metadata, over RTP.

Each camera it serves, by name, has two mounts, for the two kinds of reader:

``/<name>``
    For a viewer across a link that needs protecting, such as QGroundControl.
    UDP only, because TCP hides loss.  The viewer's RTCP drives that camera's
    rate control, and it can ask for lost packets again.
``/<name>/relay``
    For a relay on the same host, such as MediaMTX, whose link has nothing to
    protect.  TCP only, and its RTCP has no say in rate control.

What a viewer's RTCP means to rate control is :py:class:`LossMeter`'s job.  A
session holds the camera's video while it plays, so the camera idles when
nobody is watching.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import struct
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

from SpiriCamera import exif, klv
from SpiriCamera.transports import VideoPacket
from SpiriCamera.video import VIDEO_CLOCK_RATE

if TYPE_CHECKING:
    from SpiriCamera.camera import Camera

#: What a camera's mount ends in for a relay on the same host: ``/<name>/relay``.
RELAY_SUFFIX = "relay"

#: Seconds without any RTCP before a viewer's link is taken to be down.  GStreamer
#: reports every 3 to 7.5 seconds on a clean link, so this has to be longer.
RTCP_SILENCE = 8.0

#: Seconds between sender reports, and between reports to rate control.
_TICK_INTERVAL = 1.0

#: Most bytes of H.264 or KLV in one RTP packet.
_RTP_PAYLOAD_SIZE = 1200

#: Video packets kept for retransmission: over a second, at 3 Mbit/s.
_HISTORY_PACKETS = 1024

#: Bytes queued for a TCP reader before it's given up on until the next keyframe.
_TCP_BUFFER_LIMIT = 4_000_000

_PAYLOAD_H264 = 96
_PAYLOAD_KLV = 97
_PAYLOAD_RTX = 98

_RTCP_SR = 200
_RTCP_RR = 201
_RTCP_SDES = 202
_RTCP_RTPFB = 205
_RTCP_PSFB = 206
_FMT_NACK = 1
_FMT_PLI = 1
_FMT_FIR = 4

#: Seconds between 1900, where NTP counts from, and 1970.
_NTP_EPOCH = 2_208_988_800

_TRACK_ID = re.compile(r"/trackID=(\d+)")


def split_annexb(data: bytes) -> list[bytes]:
    """Split Annex B H.264 into its NAL units, without their start codes."""
    units = []
    start = data.find(b"\x00\x00\x01")
    while start >= 0:
        start += 3
        end = data.find(b"\x00\x00\x01", start)
        unit = data[start:] if end < 0 else data[start:end].rstrip(b"\x00")
        if unit:
            units.append(unit)
        start = end
    return units


def packetize_h264(unit: bytes, size: int = _RTP_PAYLOAD_SIZE) -> list[bytes]:
    """One NAL unit as RTP payloads: whole if it fits, otherwise as FU-A fragments."""
    if len(unit) <= size:
        return [unit]
    header, body = unit[0], unit[1:]
    indicator = (header & 0xE0) | 28
    payloads = []
    for offset in range(0, len(body), size):
        flags = header & 0x1F
        if offset == 0:
            flags |= 0x80
        if offset + size >= len(body):
            flags |= 0x40
        payloads.append(bytes([indicator, flags]) + body[offset : offset + size])
    return payloads


@dataclass
class RtcpFeedback:
    """What one RTCP datagram says about the streams it mentions.

    Attributes
    ----------
    reports : list[tuple[int, float]]
        ``(ssrc, fraction_lost)`` from each receiver report block.  GStreamer
        sends none alongside a NACK, so an empty list says nothing about loss.
    nacks : list[tuple[int, list[int]]]
        ``(ssrc, sequence numbers)`` asked for again.
    keyframe_requests : list[int]
        The SSRC of each stream a PLI or FIR asks for a keyframe on.
    """

    reports: list[tuple[int, float]] = field(default_factory=list)
    nacks: list[tuple[int, list[int]]] = field(default_factory=list)
    keyframe_requests: list[int] = field(default_factory=list)


def parse_rtcp(data: bytes) -> RtcpFeedback:
    """Read a compound RTCP datagram, stopping quietly at anything malformed."""
    feedback = RtcpFeedback()
    offset = 0
    while offset + 4 <= len(data):
        first, kind, words = struct.unpack_from(">BBH", data, offset)
        end = offset + (words + 1) * 4
        if first >> 6 != 2 or end > len(data):
            break
        count = first & 0x1F
        if kind in (_RTCP_RR, _RTCP_SR):
            blocks = offset + (8 if kind == _RTCP_RR else 28)
            for index in range(count):
                at = blocks + index * 24
                if at + 24 > end:
                    break
                ssrc, lost = struct.unpack_from(">IB", data, at)
                feedback.reports.append((ssrc, lost / 256))
        elif kind == _RTCP_RTPFB and count == _FMT_NACK and end - offset >= 16:
            ssrc = struct.unpack_from(">I", data, offset + 8)[0]
            sequences = []
            for at in range(offset + 12, end, 4):
                first_lost, following = struct.unpack_from(">HH", data, at)
                sequences.append(first_lost)
                sequences += [
                    (first_lost + bit + 1) & 0xFFFF for bit in range(16) if following >> bit & 1
                ]
            feedback.nacks.append((ssrc, sequences))
        elif kind == _RTCP_PSFB and count == _FMT_PLI and end - offset >= 12:
            feedback.keyframe_requests.append(struct.unpack_from(">I", data, offset + 8)[0])
        elif kind == _RTCP_PSFB and count == _FMT_FIR and end - offset >= 20:
            # FIR names its stream in its first entry, not the media SSRC field.
            feedback.keyframe_requests.append(struct.unpack_from(">I", data, offset + 12)[0])
        offset = end
    return feedback


class LossMeter:
    """Turns one viewer's RTCP into a report to rate control every tick.

    Loss comes from NACKs when there are any: they say exactly what went missing,
    at once, and count loss that retransmission goes on to repair, which is meant
    to lower the bitrate because retransmitting adds load to the link.  Receiver
    reports are the fallback for a viewer on a clean link, or one that doesn't NACK.
    A clean one vouches for all the time since the last sign of trouble, so it counts
    once for each second of that, as often as a SpiriSynq subscriber reports.

    A viewer that has gone quiet for :py:data:`RTCP_SILENCE` has lost everything,
    as far as anyone can tell.  One that has never sent RTCP has no say at all.

    Parameters
    ----------
    silence : float
        Seconds without RTCP before the link is taken to be down.
    """

    def __init__(self, silence: float = RTCP_SILENCE) -> None:
        self._silence = silence
        self._last_rtcp: float | None = None
        self._nacked: set[int] = set()
        self._reported: float | None = None
        self._clean_since = 0.0

    @property
    def committed(self) -> bool:
        """Whether this viewer has sent RTCP, which is what gives it a say."""
        return self._last_rtcp is not None

    def heard(self, now: float) -> None:
        """Note that RTCP arrived, whatever was in it."""
        if self._last_rtcp is None:
            self._clean_since = now
        self._last_rtcp = now

    def on_report(self, fraction_lost: float) -> None:
        """Take a receiver report's loss."""
        self._reported = fraction_lost

    def on_nack(self, sequences: list[int]) -> None:
        """Take sequence numbers a NACK asked for again."""
        self._nacked.update(sequences)

    def tick(self, now: float, packets_sent: int) -> list[float | None]:
        """The reports to make for this tick, each a loss, or ``None`` for just alive.

        Parameters
        ----------
        now : float
            The current time, on the clock given to :py:meth:`heard`.
        packets_sent : int
            Video packets sent since the last tick.
        """
        nacked, reported = self._nacked, self._reported
        self._nacked, self._reported = set(), None
        if nacked:
            self._clean_since = now
            return [min(1.0, len(nacked) / max(1, packets_sent))]
        if self._last_rtcp is not None and now - self._last_rtcp > self._silence:
            self._clean_since = now
            return [1.0]
        if reported is None:
            return [None]
        seconds = max(1, int(now - self._clean_since)) if reported == 0 else 1
        self._clean_since = now
        return [reported] * seconds


@dataclass
class _Track:
    """One RTP stream of a session, to wherever it was set up to go."""

    payload_type: int
    ssrc: int = field(default_factory=lambda: secrets.randbits(32))
    sequence: int = field(default_factory=lambda: secrets.randbits(16))
    packets: int = 0
    octets: int = 0
    last_timestamp: int = 0
    last_sent: float = 0.0
    is_set_up: bool = False
    rtp_destination: tuple[str, int] | None = None
    rtcp_destination: tuple[str, int] | None = None
    channel: int | None = None


class _Session:
    """One reader: its tracks, how far it has got, and what its RTCP has said."""

    def __init__(
        self,
        server: RtspServer,
        writer: asyncio.StreamWriter,
        mount: str,
        camera: Camera,
        is_relay: bool,
    ) -> None:
        self.server = server
        self.writer = writer
        self.mount = mount
        self.camera = camera
        self.is_relay = is_relay
        self.id = secrets.token_hex(8)
        self.subscriber = f"rtsp-{self.id}"
        self.video = _Track(_PAYLOAD_H264)
        self.metadata = _Track(_PAYLOAD_KLV)
        # One origin for both tracks: a KLV packet shares its frame's RTP timestamp.
        self.timestamp_origin = secrets.randbits(32)
        self.playing = False
        self.synced = False
        self.meter = LossMeter()
        self.packets_at_last_tick = 0
        self.history: dict[int, bytes] = {}
        self.rtx_sequence = secrets.randbits(16)
        self.rtx_ssrc = secrets.randbits(32)

    @property
    def tracks(self) -> tuple[_Track, _Track]:
        return self.video, self.metadata

    def send_packet(self, packet: VideoPacket) -> None:
        """Send one frame and its metadata, once this reader has a keyframe to start from."""
        if not self.playing:
            return
        if not self.synced:
            if not packet.keyframe:
                return
            self.synced = True
        timestamp = (self.timestamp_origin + packet.pts) & 0xFFFFFFFF
        if self.metadata.is_set_up:
            try:
                captured = float(packet.tags[exif.TIMESTAMP_TAG])
            except (KeyError, ValueError):
                captured = time.time()
            local_set = klv.uas_local_set(captured)
            chunks = range(0, len(local_set), _RTP_PAYLOAD_SIZE)
            for offset in chunks:
                last = offset + _RTP_PAYLOAD_SIZE >= len(local_set)
                chunk = local_set[offset : offset + _RTP_PAYLOAD_SIZE]
                self._send_rtp(self.metadata, chunk, last, timestamp)
        if self.video.is_set_up:
            payloads = [
                payload for unit in split_annexb(packet.payload) for payload in packetize_h264(unit)
            ]
            for index, payload in enumerate(payloads):
                self._send_rtp(self.video, payload, index == len(payloads) - 1, timestamp)

    def _send_rtp(self, track: _Track, payload: bytes, marker: bool, timestamp: int) -> None:
        track.sequence = (track.sequence + 1) & 0xFFFF
        track.last_timestamp, track.last_sent = timestamp, time.monotonic()
        track.packets += 1
        track.octets += len(payload)
        second_byte = track.payload_type | (0x80 if marker else 0)
        header = struct.pack(">BBHII", 0x80, second_byte, track.sequence, timestamp, track.ssrc)
        packet = header + payload
        if track.channel is not None:
            self._send_interleaved(track.channel, packet)
        elif track.rtp_destination is not None:
            if track is self.video:
                self.history[track.sequence] = packet
                self.history.pop((track.sequence - _HISTORY_PACKETS) & 0xFFFF, None)
            self.server.send_datagram(track.payload_type, False, packet, track.rtp_destination)

    def _send_interleaved(self, channel: int, data: bytes) -> None:
        if self.writer.is_closing():
            return
        transport = self.writer.transport
        if transport.get_write_buffer_size() > _TCP_BUFFER_LIMIT:
            # TCP can't drop part of a frame for itself, so skip to the next keyframe.
            if self.synced:
                self.synced = False
                self.camera.transport_video_request_keyframe()
            return
        self.writer.write(b"$" + bytes([channel]) + struct.pack(">H", len(data)) + data)

    def retransmit(self, sequences: list[int]) -> None:
        """Send packets again, as RFC 4588 says, for those still in the history."""
        destination = self.video.rtp_destination
        if destination is None:
            return
        for sequence in sequences:
            original = self.history.get(sequence)
            if original is None:
                continue
            self.rtx_sequence = (self.rtx_sequence + 1) & 0xFFFF
            marker = original[1] & 0x80
            timestamp = struct.unpack_from(">I", original, 4)[0]
            header = struct.pack(
                ">BBHII",
                0x80,
                _PAYLOAD_RTX | marker,
                self.rtx_sequence,
                timestamp,
                self.rtx_ssrc,
            )
            packet = header + struct.pack(">H", sequence) + original[12:]
            self.server.send_datagram(_PAYLOAD_H264, False, packet, destination)

    def send_sender_reports(self) -> None:
        """Send each track's clock, so a reader can line its tracks up."""
        for track in self.tracks:
            if not track.is_set_up or not track.last_sent:
                continue
            now = time.time()
            elapsed = time.monotonic() - track.last_sent
            timestamp = (track.last_timestamp + int(elapsed * VIDEO_CLOCK_RATE)) & 0xFFFFFFFF
            report = struct.pack(
                ">BBHIIIIII",
                0x80,
                _RTCP_SR,
                6,
                track.ssrc,
                (int(now) + _NTP_EPOCH) & 0xFFFFFFFF,
                int((now % 1) * (1 << 32)) & 0xFFFFFFFF,
                timestamp,
                track.packets & 0xFFFFFFFF,
                track.octets & 0xFFFFFFFF,
            )
            packet = report + _source_description(track.ssrc)
            if track.channel is not None:
                self._send_interleaved(track.channel + 1, packet)
            elif track.rtcp_destination is not None:
                self.server.send_datagram(track.payload_type, True, packet, track.rtcp_destination)

    def tick_rate_control(self, now: float) -> None:
        """Tell rate control what this reader's RTCP has said since the last tick."""
        sent = self.video.packets - self.packets_at_last_tick
        self.packets_at_last_tick = self.video.packets
        if self.is_relay or not self.meter.committed:
            return
        for fraction_lost in self.meter.tick(now, sent):
            self.camera.transport_video_report(self.subscriber, fraction_lost=fraction_lost)


def _source_description(ssrc: int) -> bytes:
    """An RTCP SDES packet naming ``ssrc``.

    Each track gets a name of its own, because a shared one asks a reader to
    synchronize them, and QGroundControl's GStreamer then never decodes a frame.
    """
    name = f"spiricamera-{ssrc:08x}".encode()
    item = bytes([1, len(name)]) + name + b"\x00"
    padded = item + b"\x00" * (-len(item) % 4)
    words = 1 + len(padded) // 4
    return struct.pack(">BBHI", 0x81, _RTCP_SDES, words, ssrc) + padded


class _RtcpEndpoint(asyncio.DatagramProtocol):
    """Takes RTCP from UDP readers, who send it to the port they were told to."""

    def __init__(self, server: RtspServer) -> None:
        self._server = server

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._server.handle_rtcp(data)


class RtspServer:
    """Serves cameras over RTSP from a thread of its own, until stopped.

    Parameters
    ----------
    cameras : Mapping[str, Camera]
        The cameras to serve, in this process, by the name of their mount.  Each
        one's readers share its one encode.
    host : str
        The address to bind.
    port : int
        The TCP port for RTSP.  ``0`` picks one, which :py:attr:`port` then gives.
    udp_port : int
        The first of four consecutive UDP ports: video's RTP and RTCP, then
        metadata's.  ``0`` picks any four, which :py:attr:`udp_ports` then gives.
    """

    def __init__(
        self,
        cameras: Mapping[str, Camera],
        host: str = "0.0.0.0",
        port: int = 8554,
        udp_port: int = 8000,
    ) -> None:
        for name in cameras:
            if not name or "/" in name or name == RELAY_SUFFIX:
                raise ValueError(f"RTSP: {name!r} can't be a mount")
        self.cameras = dict(cameras)
        self.host = host
        self.port = port
        self.udp_port = udp_port
        #: ``(video RTP, video RTCP, metadata RTP, metadata RTCP)`` once started.
        self.udp_ports: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._sessions: dict[str, _Session] = {}
        self._sockets: dict[tuple[int, bool], asyncio.DatagramTransport] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping: asyncio.Event | None = None
        self._deliverers: dict[str, Callable[[VideoPacket], None]] = {}

    def start(self) -> None:
        """Bind and start serving.

        Raises
        ------
        OSError
            If a port is taken.
        """
        started: list[BaseException | None] = []
        ready = threading.Event()

        def run() -> None:
            asyncio.run(self._serve(started, ready))

        self._thread = threading.Thread(target=run, name="rtsp", daemon=True)
        self._thread.start()
        ready.wait()
        if started[0] is not None:
            raise started[0]

    def stop(self) -> None:
        """Stop serving and end every session."""
        if self._loop is None or self._stopping is None or self._thread is None:
            return
        self._loop.call_soon_threadsafe(self._stopping.set)
        self._thread.join(timeout=5)
        self._thread = None

    async def _serve(self, started: list[BaseException | None], ready: threading.Event) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        try:
            server = await self._bind()
        except OSError as exc:
            started.append(exc)
            ready.set()
            return
        for name, camera in self.cameras.items():
            self._deliverers[name] = self._deliverer(name)
            camera.transport_video.packet_received.connect(self._deliverers[name])
        started.append(None)
        ready.set()
        ticker = asyncio.ensure_future(self._tick())
        await self._stopping.wait()
        ticker.cancel()
        for name, camera in self.cameras.items():
            camera.transport_video.packet_received.disconnect(self._deliverers.pop(name))
        for session in list(self._sessions.values()):
            self._end(session)
            session.writer.close()
        server.close()
        for socket in self._sockets.values():
            socket.close()

    async def _bind(self) -> asyncio.Server:
        loop = asyncio.get_running_loop()
        ports = []
        for index, payload_type in enumerate((_PAYLOAD_H264, _PAYLOAD_KLV)):
            for is_rtcp in (False, True):
                wanted = self.udp_port + index * 2 + is_rtcp if self.udp_port else 0
                factory = (lambda: _RtcpEndpoint(self)) if is_rtcp else asyncio.DatagramProtocol
                socket, _ = await loop.create_datagram_endpoint(
                    factory, local_addr=(self.host, wanted)
                )
                self._sockets[payload_type, is_rtcp] = socket
                ports.append(socket.get_extra_info("sockname")[1])
        self.udp_ports = tuple(ports)  # type: ignore[assignment]
        server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = server.sockets[0].getsockname()[1]
        return server

    def send_datagram(
        self, payload_type: int, is_rtcp: bool, data: bytes, destination: tuple[str, int]
    ) -> None:
        """Send from the port a reader was told this track's traffic comes from."""
        self._sockets[payload_type, is_rtcp].sendto(data, destination)

    def _deliverer(self, name: str) -> Callable[[VideoPacket], None]:
        """What hands one camera's packets to its readers."""

        def deliver(packet: VideoPacket) -> None:
            for session in list(self._sessions.values()):
                if session.camera is self.cameras[name]:
                    session.send_packet(packet)

        def on_packet(packet: VideoPacket) -> None:
            # Called on the encoder's thread; sessions live on this server's loop.
            if self._loop is not None:
                self._loop.call_soon_threadsafe(deliver, packet)

        return on_packet

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(_TICK_INTERVAL)
            now = time.monotonic()
            for session in list(self._sessions.values()):
                if session.playing:
                    session.send_sender_reports()
                    session.tick_rate_control(now)

    def handle_rtcp(self, data: bytes, sender: _Session | None = None) -> None:
        """Act on RTCP from a reader, picked out by the SSRC it's talking about.

        ``sender`` is the session it came in on, when that's known, as it is over TCP:
        a keyframe request that names no stream of ours is taken to be for it.
        """
        feedback = parse_rtcp(data)
        now = time.monotonic()
        for ssrc, fraction_lost in feedback.reports:
            session = self._session_for(ssrc)
            if session is not None:
                session.meter.heard(now)
                session.meter.on_report(fraction_lost)
        for ssrc, sequences in feedback.nacks:
            session = self._session_for(ssrc)
            if session is not None:
                session.meter.heard(now)
                session.meter.on_nack(sequences)
                session.retransmit(sequences)
        for ssrc in feedback.keyframe_requests:
            session = self._session_for(ssrc) or sender
            if session is not None:
                session.camera.transport_video_request_keyframe()

    def _session_for(self, ssrc: int) -> _Session | None:
        for session in self._sessions.values():
            if session.video.ssrc == ssrc:
                return session
        return None

    def _play(self, session: _Session) -> None:
        if session.playing:
            return
        session.playing = True
        session.camera.transport_video_hold()
        session.camera.transport_video_request_keyframe()
        logger.info(f"RTSP: {session.mount} session {session.id} playing")

    def _end(self, session: _Session) -> None:
        if self._sessions.pop(session.id, None) is None:
            return
        if session.playing:
            session.playing = False
            session.camera.transport_video_release()
            session.camera.transport_video_forget(session.subscriber)
        logger.info(f"RTSP: {session.mount} session {session.id} ended")

    def _mount(self, path: str) -> tuple[Camera, bool] | None:
        """The camera a request's path is for, and whether it's that camera's relay mount."""
        path = _TRACK_ID.sub("", "/" + path).strip("/")
        name, _, rest = path.partition("/")
        camera = self.cameras.get(name)
        if camera is None or rest not in ("", RELAY_SUFFIX):
            return None
        return camera, rest == RELAY_SUFFIX

    def _describe(self, is_relay: bool) -> str:
        """The SDP for a mount.  Only a viewer's mount offers retransmission, which needs AVPF."""
        if not is_relay:
            media = (
                f"m=video 0 RTP/AVPF {_PAYLOAD_H264} {_PAYLOAD_RTX}\r\n"
                f"a=rtpmap:{_PAYLOAD_H264} H264/{VIDEO_CLOCK_RATE}\r\n"
                f"a=fmtp:{_PAYLOAD_H264} packetization-mode=1\r\n"
                f"a=rtcp-fb:{_PAYLOAD_H264} nack\r\n"
                f"a=rtcp-fb:{_PAYLOAD_H264} nack pli\r\n"
                f"a=rtpmap:{_PAYLOAD_RTX} rtx/{VIDEO_CLOCK_RATE}\r\n"
                f"a=fmtp:{_PAYLOAD_RTX} apt={_PAYLOAD_H264};rtx-time=1000\r\n"
            )
        else:
            media = (
                f"m=video 0 RTP/AVP {_PAYLOAD_H264}\r\n"
                f"a=rtpmap:{_PAYLOAD_H264} H264/{VIDEO_CLOCK_RATE}\r\n"
                f"a=fmtp:{_PAYLOAD_H264} packetization-mode=1\r\n"
            )
        return (
            "v=0\r\n"
            "o=- 0 0 IN IP4 0.0.0.0\r\n"
            "s=SpiriCamera\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "t=0 0\r\n"
            "a=control:*\r\n"
            f"{media}"
            "a=control:trackID=0\r\n"
            f"m=application 0 RTP/AVP {_PAYLOAD_KLV}\r\n"
            f"a=rtpmap:{_PAYLOAD_KLV} smpte336m/{VIDEO_CLOCK_RATE}\r\n"
            "a=control:trackID=1\r\n"
        )

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One RTSP connection, which is one session: when it closes, the session ends."""
        session: _Session | None = None
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    channel, size = struct.unpack(">BH", await reader.readexactly(3))
                    data = await reader.readexactly(size)
                    if channel % 2:
                        self.handle_rtcp(data, session)
                    continue
                raw = first + await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode(errors="replace").split("\r\n")
                headers = {}
                for line in lines[1:]:
                    name, separator, value = line.partition(":")
                    if separator:
                        headers[name.strip().lower()] = value.strip()
                if "content-length" in headers:
                    await reader.readexactly(int(headers["content-length"]))
                session = self._request(lines[0], headers, writer, session)
                await writer.drain()
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ConnectionError,
            ValueError,
        ):
            pass
        finally:
            if session is not None:
                self._end(session)
            writer.close()

    def _request(
        self,
        request_line: str,
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
        session: _Session | None,
    ) -> _Session | None:
        """Answer one request, returning the connection's session afterwards."""
        method, url, _ = request_line.split(" ", 2)
        cseq = headers.get("cseq", "0")
        path = url.split("//", 1)[-1].partition("/")[2].partition("?")[0]
        mount = _TRACK_ID.sub("", "/" + path).strip("/")
        found = self._mount(path)

        def respond(status: str, extra: str = "", body: str = "") -> None:
            length = f"Content-Length: {len(body)}\r\n" if body else ""
            text = f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{extra}{length}\r\n{body}"
            writer.write(text.encode())

        session_header = f"Session: {session.id};timeout=60\r\n" if session else ""

        if method == "OPTIONS":
            respond("200 OK", "Public: OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN\r\n")
        elif method == "DESCRIBE":
            if found is None:
                respond("404 Not Found")
            else:
                respond(
                    "200 OK",
                    f"Content-Type: application/sdp\r\nContent-Base: {url.rstrip('/')}/\r\n",
                    self._describe(found[1]),
                )
        elif method == "SETUP":
            if found is None:
                respond("404 Not Found")
                return session
            return self._setup(url, mount, *found, headers, writer, session, respond)
        elif method == "PLAY":
            if session is None:
                respond("454 Session Not Found")
            else:
                info = ",".join(
                    f"{url.rstrip('/')}/trackID={index};seq={(track.sequence + 1) & 0xFFFF}"
                    for index, track in enumerate(session.tracks)
                    if track.is_set_up
                )
                # Holding first, so a reader told it's playing already counts as demand.
                # No await between them, so no frame can overtake this response.
                self._play(session)
                respond("200 OK", f"{session_header}RTP-Info: {info}\r\n")
        elif method == "GET_PARAMETER":
            respond("200 OK", session_header)
        elif method == "TEARDOWN":
            respond("200 OK", session_header)
            if session is not None:
                self._end(session)
            return None
        else:
            respond("501 Not Implemented")
        return session

    def _setup(
        self,
        url: str,
        mount: str,
        camera: Camera,
        is_relay: bool,
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
        session: _Session | None,
        respond: Callable[..., None],
    ) -> _Session | None:
        """Set up one track, over the only transport its mount allows."""
        transport = headers.get("transport", "").split(",")[0]
        options = transport.split(";")
        is_tcp = options[0].endswith("/TCP")
        client_ports = next((o[12:] for o in options if o.startswith("client_port=")), "")
        wanted_track = _TRACK_ID.search(url)
        if is_tcp != is_relay or not wanted_track or not (is_tcp or client_ports):
            respond("461 Unsupported Transport")
            return session
        if session is not None and session.mount != mount:
            respond("459 Aggregate Operation Not Allowed")
            return session
        if session is None:
            session = _Session(self, writer, mount, camera, is_relay)
            self._sessions[session.id] = session
        index = int(wanted_track.group(1))
        if index >= len(session.tracks):
            respond("404 Not Found")
            return session
        track = session.tracks[index]
        track.is_set_up = True
        if is_tcp:
            track.channel = index * 2
            reply = (
                f"RTP/AVP/TCP;unicast;interleaved={track.channel}-{track.channel + 1};"
                f"ssrc={track.ssrc:08x}"
            )
        else:
            low, _, high = client_ports.partition("-")
            # Only ever to whoever asked, so this can't aim traffic at someone else.
            peer = writer.get_extra_info("peername")[0]
            track.rtp_destination = (peer, int(low))
            track.rtcp_destination = (peer, int(high or int(low) + 1))
            server_rtp = self.udp_ports[index * 2]
            server_rtcp = self.udp_ports[index * 2 + 1]
            reply = (
                f"{options[0]};unicast;client_port={low}-{track.rtcp_destination[1]};"
                f"server_port={server_rtp}-{server_rtcp};ssrc={track.ssrc:08x}"
            )
        respond("200 OK", f"Transport: {reply}\r\nSession: {session.id};timeout=60\r\n")
        return session


__all__ = [
    "RELAY_SUFFIX",
    "RTCP_SILENCE",
    "LossMeter",
    "RtcpFeedback",
    "RtspServer",
    "packetize_h264",
    "parse_rtcp",
    "split_annexb",
]
