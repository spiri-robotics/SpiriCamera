"""Tests for source URL parsing and handler resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from SpiriCamera.sources import (
    SourceError,
    SourceURL,
    UnknownSourceError,
    describe_source,
    known_schemes,
    registered_sources,
    resolve_source,
)
from SpiriCamera.sources.base import SourceBase, SourceInfo
from SpiriCamera.sources.file import FileSource
from SpiriCamera.sources.network import NetworkSource
from SpiriCamera.sources.testimage import TestImageSource as ImageSource
from SpiriCamera.sources.v4l import V4LSource


class TestSourceURL:
    """Splitting a source string into scheme and target."""

    @pytest.mark.parametrize(
        ("raw", "scheme", "target"),
        [
            ("testimage://pm5544", "testimage", "pm5544"),
            ("testimage://", "testimage", ""),
            ("rtsp://host/stream", "rtsp", "host/stream"),
            ("RTSP://host/stream", "rtsp", "host/stream"),
            ("/dev/video0", "", "/dev/video0"),
            ("0", "", "0"),
            ("  /dev/video1  ", "", "/dev/video1"),
        ],
    )
    def test_parse(self, raw: str, scheme: str, target: str) -> None:
        """Schemes are lowercased and schemeless strings keep their target."""
        url = SourceURL.parse(raw)
        assert url.scheme == scheme
        assert url.target == target
        assert url.raw == raw.strip()

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_parse_rejects_empty(self, raw: str) -> None:
        """An empty source string is an error, not an empty URL."""
        with pytest.raises(SourceError, match="cannot be empty"):
            SourceURL.parse(raw)


class TestResolution:
    """Picking the right handler for a source string."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("testimage://", ImageSource),
            ("testimage://pm5544", ImageSource),
            ("0", V4LSource),
            ("3", V4LSource),
            ("12", V4LSource),
            ("v4l://0", V4LSource),
            ("v4l2:///dev/video2", V4LSource),
            ("v4l:///dev/video10", V4LSource),
            ("rtsp://host/stream", NetworkSource),
            ("rtmp://host/live", NetworkSource),
            ("http://host/feed.mjpg", NetworkSource),
            ("https://host/feed.mjpg", NetworkSource),
        ],
    )
    def test_resolves_to_handler(self, source: str, expected: type[SourceBase]) -> None:
        """Each supported form lands on the handler that owns it."""
        assert isinstance(resolve_source(source), expected)

    @pytest.mark.parametrize(
        ("source", "match"),
        [
            ("", "cannot be empty"),
            ("nope", "Unrecognised source"),
            ("ftp://host/file", "Unrecognised source"),
            ("testimage://missing", "Unknown test image"),
            ("rtsp://", "No host given"),
            ("v4l://", "No V4L2 device"),
            ("file://", "No file given"),
            ("file:///no/such/clip.mp4", "No such file"),
            ("/dev/vi", "Unrecognised source"),
            ("/dev/null", "Unrecognised source"),
            ("/tmp", "Unrecognised source"),
        ],
    )
    def test_rejects(self, source: str, match: str) -> None:
        """Bad sources fail at resolution with a usable message.

        A half-typed path is rejected outright rather than claimed and
        then failing to open, which is what lets the UI say why.
        """
        with pytest.raises(SourceError, match=match):
            resolve_source(source)

    def test_unknown_scheme_is_a_subclass_of_source_error(self) -> None:
        """Callers can catch UnknownSourceError or the base class."""
        with pytest.raises(UnknownSourceError):
            resolve_source("ftp://host/file")
        assert issubclass(UnknownSourceError, SourceError)

    def test_explicit_scheme_wins_over_pattern_match(self) -> None:
        """A claimed scheme beats a handler that matches schemeless forms."""
        assert isinstance(resolve_source("rtsp:///dev/video0"), NetworkSource)

    def test_file_scheme_disambiguates_a_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path that reads as a URL can be forced to mean the file.

        POSIX collapses repeated slashes, so the relative path
        ``rtsp://clip.mp4`` really does name ``clip.mp4`` inside a
        directory called ``rtsp:``.  It is indistinguishable from a
        stream URL, and the stream wins; ``file://`` is how you say you
        meant the file.
        """
        directory = tmp_path / "rtsp:"
        directory.mkdir()
        (directory / "clip.mp4").write_bytes(b"not really a video")
        monkeypatch.chdir(tmp_path)

        assert isinstance(resolve_source("rtsp://clip.mp4"), NetworkSource)
        assert isinstance(resolve_source("file://rtsp://clip.mp4"), FileSource)

    def test_error_message_lists_supported_forms(self) -> None:
        """The failure tells the user what they could have typed."""
        with pytest.raises(SourceError) as caught:
            resolve_source("nope")
        message = str(caught.value)
        assert "testimage://" in message
        assert "camera index" in message


class TestRegistry:
    """The registry of handlers."""

    def test_finds_concrete_handlers(self) -> None:
        """Every shipped handler is registered by import alone."""
        registered = registered_sources()
        assert FileSource in registered
        assert ImageSource in registered
        assert V4LSource in registered
        assert NetworkSource in registered

    def test_excludes_abstract_bases(self) -> None:
        """Abstract classes are not offered as handlers."""
        from SpiriCamera.sources.capture import OpenCVSource

        assert SourceBase not in registered_sources()
        assert OpenCVSource not in registered_sources()

    def test_known_schemes(self) -> None:
        """Schemes are reported sorted and suffixed."""
        schemes = known_schemes()
        assert schemes == sorted(schemes)
        assert "testimage://" in schemes
        assert "rtsp://" in schemes


class TestDescribe:
    """Describing a source without opening it."""

    def test_describes_network_source(self) -> None:
        """A description carries scheme, target, and handler."""
        info = describe_source("rtsp://host/stream")
        assert isinstance(info, SourceInfo)
        assert info.url == "rtsp://host/stream"
        assert info.scheme == "rtsp"
        assert info.target == "host/stream"
        assert info.handler == "NetworkSource"
        assert info.error == ""

    def test_fills_in_scheme_for_schemeless_source(self) -> None:
        """A bare device path still reports the scheme it resolved to."""
        info = describe_source("v4l:///dev/video0")
        assert info.scheme == "v4l"
        assert info.target == "/dev/video0"

    def test_testimage_reports_default_pattern(self) -> None:
        """An unnamed test image describes the pattern it will serve."""
        assert describe_source("testimage://").target == "pm5544"


class TestSourceInfo:
    """The synced source description."""

    def test_unresolved_carries_the_error(self) -> None:
        """A failed resolution is representable, not just raisable."""
        info = SourceInfo.unresolved("bogus://x", "nope")
        assert info.url == "bogus://x"
        assert info.error == "nope"
        assert info.handler == ""

    def test_update_mutates_in_place(self) -> None:
        """Updating keeps object identity so bindings survive."""
        info = SourceInfo(url="old", scheme="a", error="stale")
        original = info

        info.update(describe_source("testimage://"))

        assert info is original
        assert info.scheme == "testimage"
        assert info.url == "testimage://"
        assert info.error == ""
