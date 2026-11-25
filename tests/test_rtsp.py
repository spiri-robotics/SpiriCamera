"""Tests for the RTSP server and the KLV it sends.

The server runs for real on loopback, with a real camera behind it. Only the
client is minimal: enough RTSP and RTP to check what's on the wire.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import AsyncIterator, Callable

import pytest

from SpiriCamera import klv
from SpiriCamera.rtsp import (
    RTCP_SILENCE,
    LossMeter,
    RtspServer,
    _source_description,
    packetize_h264,
    parse_rtcp,
    split_annexb,
)

#: The camera these tests serve, and its two mounts.
CAMERA_MOUNT = "raw"
RELAY_MOUNT = "raw/relay"


class TestAnnexB:
    def test_splits_three_and_four_byte_start_codes(self) -> None:
        data = b"\x00\x00\x00\x01\x67\x42\x00\x00\x01\x68\xce\x00\x00\x00\x01\x65\x88"
        assert split_annexb(data) == [b"\x67\x42", b"\x68\xce", b"\x65\x88"]

    def test_nothing_without_a_start_code(self) -> None:
        assert split_annexb(b"\x67\x42") == []


class TestPacketize:
    def test_a_small_unit_is_sent_whole(self) -> None:
        assert packetize_h264(b"\x65" + b"x" * 10, size=100) == [b"\x65" + b"x" * 10]

    def test_a_large_unit_reassembles_from_fu_a(self) -> None:
        unit = b"\x65" + bytes(range(250))
        payloads = packetize_h264(unit, size=100)

        assert len(payloads) == 3
        assert all(p[0] == (0x65 & 0xE0) | 28 for p in payloads)
        assert [p[1] & 0xC0 for p in payloads] == [0x80, 0x00, 0x40]
        assert all(p[1] & 0x1F == 0x65 & 0x1F for p in payloads)
        assert b"".join(p[2:] for p in payloads) == unit[1:]


def _rr(ssrc: int, lost: int) -> bytes:
    """A receiver report with one block."""
    block = struct.pack(">IB", ssrc, lost) + bytes(19)
    return struct.pack(">BBHI", 0x81, 201, 7, 1234) + block


def _empty_rr() -> bytes:
    """The report GStreamer sends alongside a NACK, with no blocks."""
    return struct.pack(">BBHI", 0x80, 201, 1, 1234)


def _nack(ssrc: int, first: int, following: int = 0) -> bytes:
    return struct.pack(">BBHIIHH", 0x81, 205, 3, 1234, ssrc, first, following)


def _pli(ssrc: int) -> bytes:
    return struct.pack(">BBHII", 0x81, 206, 2, 1234, ssrc)


def _fir(ssrc: int) -> bytes:
    return struct.pack(">BBHIIIBxxx", 0x84, 206, 4, 1234, 0, ssrc, 1)


class TestSourceDescription:
    def test_each_track_gets_a_name_of_its_own(self) -> None:
        """A shared CNAME asks a reader to sync them, and QGC then never decodes."""
        assert _source_description(1)[8:] != _source_description(2)[8:]

    def test_is_a_whole_number_of_words(self) -> None:
        packet = _source_description(1)
        words = struct.unpack_from(">H", packet, 2)[0]
        assert len(packet) == (words + 1) * 4
        assert len(packet) % 4 == 0


class TestParseRtcp:
    def test_receiver_report(self) -> None:
        assert parse_rtcp(_rr(7, 64)).reports == [(7, 0.25)]

    def test_nack_expands_its_bitmask(self) -> None:
        feedback = parse_rtcp(_empty_rr() + _nack(7, 100, 0b101))
        assert feedback.reports == []
        assert feedback.nacks == [(7, [100, 101, 103])]

    def test_nack_sequence_numbers_wrap(self) -> None:
        assert parse_rtcp(_nack(7, 0xFFFF, 0b1)).nacks == [(7, [0xFFFF, 0])]

    def test_pli_asks_for_a_keyframe_on_its_stream(self) -> None:
        assert parse_rtcp(_pli(7)).keyframe_requests == [7]

    def test_fir_names_its_stream_in_its_entry(self) -> None:
        assert parse_rtcp(_fir(7)).keyframe_requests == [7]

    @pytest.mark.parametrize("data", [b"", b"\x81", b"\x00" * 8, _rr(7, 64)[:12]])
    def test_malformed_is_ignored(self, data: bytes) -> None:
        feedback = parse_rtcp(data)
        assert not feedback.reports and not feedback.nacks and not feedback.keyframe_requests


class _FullSocket:
    """A UDP socket whose kernel send buffer is full."""

    def sendto(self, data: bytes, destination: tuple[str, int]) -> int:
        raise BlockingIOError


class TestSendDatagram:
    def test_drops_a_packet_the_kernel_has_no_room_for(self) -> None:
        server = RtspServer({})
        server._sockets[96, False] = _FullSocket()  # type: ignore[assignment]
        server.send_datagram(96, False, b"packet", ("127.0.0.1", 9))
        server.send_datagram(96, False, b"packet", ("127.0.0.1", 9))
        assert server.dropped == 2


class TestLossMeter:
    def test_has_no_say_until_it_hears_rtcp(self) -> None:
        meter = LossMeter()
        assert not meter.committed
        meter.heard(0.0)
        assert meter.committed

    def test_alive_with_nothing_to_say_reports_no_loss_figure(self) -> None:
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        assert meter.tick(1.0, 100) == [None]

    def test_passes_on_a_receiver_report_once(self) -> None:
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        meter.on_report(0.25)
        assert meter.tick(1.0, 100) == [0.25]
        assert meter.tick(2.0, 100) == [None]

    def test_a_clean_report_counts_for_each_second_it_covers(self) -> None:
        """Otherwise a slow reporter's estimate would take minutes to recover."""
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        meter.tick(1.0, 100)
        meter.heard(5.0)
        meter.on_report(0.0)
        assert meter.tick(5.0, 100) == [0.0] * 5

    def test_a_clean_report_only_covers_since_the_last_trouble(self) -> None:
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        meter.on_nack([1])
        meter.tick(3.0, 100)
        meter.heard(5.0)
        meter.on_report(0.0)
        assert meter.tick(5.0, 100) == [0.0] * 2

    def test_a_clean_report_vouches_for_no_part_of_an_outage(self) -> None:
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        assert meter.tick(20.0, 100) == [1.0]
        meter.heard(20.5)
        meter.on_report(0.0)
        assert meter.tick(21.0, 100) == [0.0]

    def test_loss_from_nacks_counts_each_packet_once(self) -> None:
        """Repaired loss counts, so a NACK is enough, however often it repeats."""
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        meter.on_nack([1, 2, 3])
        meter.on_nack([2, 3, 4, 5])
        assert meter.tick(1.0, 100) == [0.05]

    def test_nacks_outrank_a_receiver_report(self) -> None:
        """A report only counts what retransmission didn't repair."""
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        meter.on_report(0.0)
        meter.on_nack([1, 2])
        assert meter.tick(1.0, 10) == [0.2]

    def test_loss_never_exceeds_everything(self) -> None:
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        meter.on_nack([1, 2, 3])
        assert meter.tick(1.0, 0) == [1.0]

    def test_silence_is_total_loss(self) -> None:
        meter = LossMeter(silence=8)
        meter.heard(0.0)
        assert meter.tick(8.0, 100) == [None]
        assert meter.tick(8.1, 100) == [1.0]
        meter.heard(9.0)
        assert meter.tick(10.0, 100) == [None]

    def test_default_silence_outlasts_gstreamers_slowest_reports(self) -> None:
        assert RTCP_SILENCE > 7.5


class TestKlv:
    def test_a_local_set_is_well_formed(self) -> None:
        packet = klv.uas_local_set(1_700_000_000.25)

        assert packet.startswith(klv.UAS_LOCAL_SET_KEY)
        length = packet[16]
        assert length == len(packet) - 17
        items = _items(packet)
        assert struct.unpack(">Q", items[2])[0] == 1_700_000_000_250_000
        assert items[65] == bytes([klv.ST0601_VERSION])

    def test_the_checksum_comes_last_and_covers_up_to_its_value(self) -> None:
        packet = klv.uas_local_set(1.0)
        assert packet[-4:-2] == bytes([1, 2])
        total = sum(b << (8 * ((i + 1) % 2)) for i, b in enumerate(packet[:-2])) & 0xFFFF
        assert struct.unpack(">H", packet[-2:])[0] == total


def _items(packet: bytes) -> dict[int, bytes]:
    items, at = {}, 17
    while at < len(packet):
        tag, size = packet[at], packet[at + 1]
        items[tag] = packet[at + 2 : at + 2 + size]
        at += 2 + size
    return items


class _Rtsp:
    """A minimal RTSP client."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int):
        self.reader, self.writer, self.port = reader, writer, port
        self.cseq = 0
        self.session = ""

    @classmethod
    async def connect(cls, port: int) -> _Rtsp:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return cls(reader, writer, port)

    async def request(self, method: str, path: str, **headers: str) -> tuple[int, str, str]:
        self.cseq += 1
        lines = [f"{method} rtsp://127.0.0.1:{self.port}/{path} RTSP/1.0", f"CSeq: {self.cseq}"]
        if self.session:
            lines.append(f"Session: {self.session}")
        lines += [f"{name}: {value}" for name, value in headers.items()]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.writer.drain()
        return await self._response()

    async def _response(self) -> tuple[int, str, str]:
        head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), 5)
        text = head.decode()
        status = int(text.split(" ", 2)[1])
        length = 0
        for line in text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":")[1])
            if line.lower().startswith("session:"):
                self.session = line.split(":")[1].split(";")[0].strip()
        body = (await self.reader.readexactly(length)).decode() if length else ""
        return status, text, body

    def close(self) -> None:
        self.writer.close()


class _Udp(asyncio.DatagramProtocol):
    """Collects whatever arrives on one port."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets.append(data)

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]  # type: ignore[union-attr]


async def _udp() -> _Udp:
    _, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        _Udp, local_addr=("127.0.0.1", 0)
    )
    return protocol


async def _until(condition: Callable[[], bool], seconds: float = 8.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


def _ssrc(response: str) -> int:
    return int(response.split("ssrc=")[1].split("\r\n")[0].split(";")[0], 16)


def _server_ports(response: str) -> tuple[int, int]:
    low, high = response.split("server_port=")[1].split(";")[0].split("-")
    return int(low), int(high)


@pytest.fixture
async def served(camera) -> AsyncIterator[tuple[object, RtspServer]]:
    cam = camera(max_width=160, max_height=120)
    cam.start()
    server = RtspServer({CAMERA_MOUNT: cam}, host="127.0.0.1", port=0, udp_port=0)
    server.start()
    try:
        yield cam, server
    finally:
        server.stop()


async def _play_udp(server: RtspServer) -> tuple[_Rtsp, dict[str, object]]:
    """Set up and play both tracks over UDP, returning what a test needs to poke them."""
    client = await _Rtsp.connect(server.port)
    video, metadata = await _udp(), await _udp()
    _, video_setup, _ = await client.request(
        "SETUP",
        f"{CAMERA_MOUNT}/trackID=0",
        Transport=f"RTP/AVPF;unicast;client_port={video.port}-{video.port + 1}",
    )
    await client.request(
        "SETUP",
        f"{CAMERA_MOUNT}/trackID=1",
        Transport=f"RTP/AVP;unicast;client_port={metadata.port}-{metadata.port + 1}",
    )
    status, _, _ = await client.request("PLAY", CAMERA_MOUNT)
    assert status == 200
    return client, {
        "video": video,
        "metadata": metadata,
        "ssrc": _ssrc(video_setup),
        "rtcp_port": _server_ports(video_setup)[1],
    }


class TestSignalling:
    @pytest.mark.parametrize("mount", [CAMERA_MOUNT, RELAY_MOUNT])
    async def test_describe_offers_h264_and_klv(self, served, mount: str) -> None:
        _, server = served
        client = await _Rtsp.connect(server.port)
        status, _, sdp = await client.request("DESCRIBE", mount, Accept="application/sdp")
        client.close()

        assert status == 200
        assert "H264/90000" in sdp
        assert "smpte336m/90000" in sdp

    async def test_only_the_camera_mount_offers_retransmission(self, served) -> None:
        _, server = served
        client = await _Rtsp.connect(server.port)
        _, _, camera_sdp = await client.request("DESCRIBE", CAMERA_MOUNT)
        _, _, relay_sdp = await client.request("DESCRIBE", RELAY_MOUNT)
        client.close()

        assert "rtx/90000" in camera_sdp and "nack" in camera_sdp
        assert "rtx/90000" not in relay_sdp

    @pytest.mark.parametrize("mount", ["nope", "nope/relay", f"{CAMERA_MOUNT}/nope", "relay"])
    async def test_an_unknown_mount_is_not_found(self, served, mount: str) -> None:
        _, server = served
        client = await _Rtsp.connect(server.port)
        status, _, _ = await client.request("DESCRIBE", mount)
        _, _, _ = await client.request("OPTIONS", "")
        status_setup, _, _ = await client.request(
            "SETUP", f"{mount}/trackID=0", Transport="RTP/AVP/TCP;unicast;interleaved=0-1"
        )
        client.close()
        assert status == 404
        assert status_setup == 404

    @pytest.mark.parametrize("name", ["", "a/b", "relay"])
    def test_a_name_that_cant_be_a_mount_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError):
            RtspServer({name: object()})  # type: ignore[dict-item]

    async def test_the_camera_mount_refuses_tcp(self, served) -> None:
        """TCP hides loss, so a viewer falling back to it would blind rate control."""
        _, server = served
        client = await _Rtsp.connect(server.port)
        status, _, _ = await client.request(
            "SETUP",
            f"{CAMERA_MOUNT}/trackID=0",
            Transport="RTP/AVP/TCP;unicast;interleaved=0-1",
        )
        client.close()
        assert status == 461

    async def test_the_relay_mount_refuses_udp(self, served) -> None:
        _, server = served
        client = await _Rtsp.connect(server.port)
        status, _, _ = await client.request(
            "SETUP",
            f"{RELAY_MOUNT}/trackID=0",
            Transport="RTP/AVP;unicast;client_port=5000-5001",
        )
        client.close()
        assert status == 461

    async def test_play_without_setup_has_no_session(self, served) -> None:
        _, server = served
        client = await _Rtsp.connect(server.port)
        status, _, _ = await client.request("PLAY", CAMERA_MOUNT)
        client.close()
        assert status == 454

    async def test_rubbish_only_ends_that_connection(self, served) -> None:
        _, server = served
        bad = await _Rtsp.connect(server.port)
        bad.writer.write(b"rubbish\r\n\r\n")
        await bad.writer.drain()
        assert await asyncio.wait_for(bad.reader.read(), 5) == b""

        client = await _Rtsp.connect(server.port)
        status, _, _ = await client.request("OPTIONS", CAMERA_MOUNT)
        client.close()
        assert status == 200


class TestUdpPlayback:
    async def test_sends_video_with_metadata_on_its_timestamps(self, served) -> None:
        _, server = served
        client, ports = await _play_udp(server)
        video, metadata = ports["video"], ports["metadata"]
        await _until(lambda: len(video.packets) > 3 and len(metadata.packets) > 3)
        client.close()

        video_stamps = {struct.unpack_from(">I", p, 4)[0] for p in video.packets}
        klv_stamps = [struct.unpack_from(">I", p, 4)[0] for p in metadata.packets]
        assert all(p[12:].startswith(klv.UAS_LOCAL_SET_KEY) for p in metadata.packets)
        assert all(p[1] & 0x7F == 97 and p[1] & 0x80 for p in metadata.packets)
        # A KLV packet goes first, so only the latest may not have its frame yet.
        assert set(klv_stamps[:-1]) <= video_stamps

    async def test_starts_from_a_keyframe(self, served) -> None:
        _, server = served
        client, ports = await _play_udp(server)
        video = ports["video"]
        await _until(lambda: bool(video.packets))
        client.close()

        first = video.packets[0][12]
        assert first & 0x1F == 7 or (first & 0x1F == 28 and video.packets[0][13] & 0x1F == 5)

    async def test_holds_the_camera_only_while_playing(self, served) -> None:
        cam, server = served
        assert not cam._video_demanded()
        client, _ = await _play_udp(server)
        assert cam._video_demanded()
        client.close()
        await _until(lambda: not cam._video_demanded())

    async def test_retransmits_what_a_nack_asks_for(self, served) -> None:
        _, server = served
        client, ports = await _play_udp(server)
        video = ports["video"]
        await _until(lambda: bool(video.packets))
        original = video.packets[0]
        sequence = struct.unpack_from(">H", original, 2)[0]

        video.transport.sendto(_nack(ports["ssrc"], sequence), ("127.0.0.1", ports["rtcp_port"]))
        rtx = lambda: [p for p in video.packets if p[1] & 0x7F == 98]  # noqa: E731
        await _until(lambda: bool(rtx()))
        client.close()

        assert struct.unpack_from(">H", rtx()[0], 12)[0] == sequence
        assert rtx()[0][14:] == original[12:]
        assert rtx()[0][4:8] == original[4:8]


class TestRateControl:
    @pytest.fixture
    def reports(self, served, monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
        cam, _ = served
        seen: list[float | None] = []
        real = cam.transport_video_report

        def spy(subscriber: str, *, fraction_lost=None, max_bitrate=None) -> None:
            seen.append(fraction_lost)
            real(subscriber, fraction_lost=fraction_lost, max_bitrate=max_bitrate)

        monkeypatch.setattr(cam, "transport_video_report", spy)
        return seen

    async def test_a_viewer_with_no_rtcp_has_no_say(self, served, reports) -> None:
        _, server = served
        client, ports = await _play_udp(server)
        await _until(lambda: bool(ports["video"].packets))
        await asyncio.sleep(1.3)
        client.close()
        assert reports == []

    async def test_a_receiver_report_reaches_rate_control(self, served, reports) -> None:
        cam, server = served
        client, ports = await _play_udp(server)
        ports["video"].transport.sendto(_rr(ports["ssrc"], 128), ("127.0.0.1", ports["rtcp_port"]))
        await _until(lambda: 0.5 in reports)
        start = cam.transport_video.rate_control.start_bitrate
        # The encoder publishes its target as it encodes the next frame.
        await _until(lambda: cam.transport_video.target_bitrate < start)
        client.close()

    async def test_repaired_loss_lowers_the_bitrate(self, served, reports) -> None:
        """A NACK counts even with no receiver report, as GStreamer sends then."""
        _, server = served
        client, ports = await _play_udp(server)
        video = ports["video"]
        await _until(lambda: bool(video.packets))
        sequence = struct.unpack_from(">H", video.packets[0], 2)[0]
        video.transport.sendto(
            _empty_rr() + _nack(ports["ssrc"], sequence), ("127.0.0.1", ports["rtcp_port"])
        )
        await _until(lambda: any(r for r in reports))
        client.close()
        assert max(r for r in reports if r) > 0

    async def test_a_slow_reporter_stays_alive_between_reports(self, served, reports) -> None:
        """GStreamer's gaps outlast stall_timeout; a tick has to fill them in."""
        _, server = served
        client, ports = await _play_udp(server)
        ports["video"].transport.sendto(_rr(ports["ssrc"], 0), ("127.0.0.1", ports["rtcp_port"]))
        await _until(lambda: reports.count(None) >= 2)
        client.close()

    async def test_leaving_takes_it_out_of_rate_control(self, served, reports) -> None:
        cam, server = served
        client, ports = await _play_udp(server)
        ports["video"].transport.sendto(_rr(ports["ssrc"], 0), ("127.0.0.1", ports["rtcp_port"]))
        await _until(lambda: bool(reports))
        assert cam._video_encoder._rates
        client.close()
        await _until(lambda: not cam._video_encoder._rates)


class TestRelay:
    async def test_interleaved_playback_and_no_say_in_rate_control(self, served) -> None:
        cam, server = served
        client = await _Rtsp.connect(server.port)
        _, setup, _ = await client.request(
            "SETUP",
            f"{RELAY_MOUNT}/trackID=0",
            Transport="RTP/AVP/TCP;unicast;interleaved=0-1",
        )
        await client.request(
            "SETUP",
            f"{RELAY_MOUNT}/trackID=1",
            Transport="RTP/AVP/TCP;unicast;interleaved=2-3",
        )
        status, _, _ = await client.request("PLAY", RELAY_MOUNT)
        assert status == 200

        channels: set[int] = set()
        deadline = time.monotonic() + 8
        while channels < {0, 2} and time.monotonic() < deadline:
            magic = await asyncio.wait_for(client.reader.readexactly(1), 5)
            assert magic == b"$"
            channel, size = struct.unpack(">BH", await client.reader.readexactly(3))
            await client.reader.readexactly(size)
            channels.add(channel)
            if channel == 0:
                report = _rr(_ssrc(setup), 255)
                client.writer.write(b"$\x01" + struct.pack(">H", len(report)) + report)
        await asyncio.sleep(1.2)
        client.close()

        assert {0, 2} <= channels
        assert not cam._video_encoder._rates


class TestSeveralCameras:
    @pytest.fixture
    async def two(self, camera) -> AsyncIterator[tuple[object, object, RtspServer]]:
        first = camera(max_width=160, max_height=120)
        second = camera(max_width=160, max_height=120)
        first.start()
        second.start()
        server = RtspServer(
            {"first": first, "second": second}, host="127.0.0.1", port=0, udp_port=0
        )
        server.start()
        try:
            yield first, second, server
        finally:
            server.stop()

    async def test_a_reader_runs_and_gets_only_its_own_camera(self, two) -> None:
        first, second, server = two
        client = await _Rtsp.connect(server.port)
        await client.request(
            "SETUP", "second/relay/trackID=0", Transport="RTP/AVP/TCP;unicast;interleaved=0-1"
        )
        status, _, _ = await client.request("PLAY", "second/relay")
        assert status == 200
        assert second._video_demanded()
        assert not first._video_demanded()

        first_packets: list[object] = []
        first.transport_video.packet_received.connect(first_packets.append)
        magic = await asyncio.wait_for(client.reader.readexactly(1), 8)
        client.close()
        first.transport_video.packet_received.disconnect(first_packets.append)

        assert magic == b"$"
        # The first camera never ran, so nothing of it can have been sent.
        assert first_packets == []
        await _until(lambda: not second._video_demanded())

    async def test_one_session_cant_mix_cameras(self, two) -> None:
        _, _, server = two
        client = await _Rtsp.connect(server.port)
        await client.request(
            "SETUP", "first/relay/trackID=0", Transport="RTP/AVP/TCP;unicast;interleaved=0-1"
        )
        status, _, _ = await client.request(
            "SETUP", "second/relay/trackID=1", Transport="RTP/AVP/TCP;unicast;interleaved=2-3"
        )
        client.close()
        assert status == 459
