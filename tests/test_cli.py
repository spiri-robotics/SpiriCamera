"""Tests for the SpiriCamera CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from SpiriCamera.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_ambient_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's SPIRICAMERA_SOURCE out of the tests."""
    monkeypatch.delenv("SPIRICAMERA_SOURCE", raising=False)


class TestBasics:
    """Commands that need no camera."""

    def test_help(self) -> None:
        """The top-level help lists the app."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "SpiriCamera" in result.stdout

    def test_version(self) -> None:
        """Version reports the package and Python versions."""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "SpiriCamera v" in result.stdout

    def test_sources_lists_handlers(self) -> None:
        """Every registered handler and its schemes are listed."""
        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0
        assert "TestImageSource" in result.stdout
        assert "testimage://" in result.stdout
        assert "V4LSource" in result.stdout


class TestMissingSource:
    """Behaviour when no source is given anywhere."""

    @pytest.mark.parametrize("command", ["validate", "run", "capture"])
    def test_reports_usage_error(self, command: str) -> None:
        """Each command explains how to supply a source."""
        result = runner.invoke(app, [command])
        assert result.exit_code == 1
        assert "SPIRICAMERA_SOURCE" in result.output

    def test_falls_back_to_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unspecified source comes from the environment."""
        monkeypatch.setenv("SPIRICAMERA_SOURCE", "testimage://")
        result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0
        assert "TestImageSource" in result.stdout


class TestRun:
    """The run command."""

    @staticmethod
    def _topics(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
        """Run ``run`` with cameras that never start, keyed by their topics."""
        from SpiriCamera import cli
        from SpiriCamera.camera import CameraError

        made: dict[str, dict] = {}

        class StopsAtStart:
            def __init__(self, **kwargs) -> None:
                made[kwargs["synq_topic"]] = kwargs
                self.overlay_widgets = kwargs["overlay_widgets"] = {}

            def start(self) -> None:
                raise CameraError("not started in tests")

            def stop(self) -> None:
                pass

        monkeypatch.setattr(cli, "Camera", StopsAtStart)
        monkeypatch.setattr(cli, "_set_lens", lambda cam: None)
        monkeypatch.setattr(cli, "_set_mount", lambda cam: None)
        runner.invoke(app, ["run", "testimage://"])
        return made

    def test_publishes_raw_hud_and_preview(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """By default the cameras are cameras/1/raw, hud and preview."""
        monkeypatch.delenv("SPIRICAMERA_TOPIC", raising=False)
        monkeypatch.delenv("SPIRICAMERA_CAMERA_NAME", raising=False)
        made = self._topics(monkeypatch)
        assert set(made) == {"cameras/1/raw", "cameras/1/hud", "cameras/1/preview"}
        assert made["cameras/1/raw"]["source"] == "testimage://"
        for relay in ("cameras/1/hud", "cameras/1/preview"):
            assert made[relay]["source"] == "spirisynq+raw://./cameras/1/raw"
        assert made["cameras/1/preview"]["max_height"] == 240
        assert made["cameras/1/preview"]["max_framerate"] == 1

    def test_hud_widgets_go_on_the_hud_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SPIRICAMERA_HUD_WIDGETS names widgets by topic, by spaces or commas."""
        monkeypatch.delenv("SPIRICAMERA_TOPIC", raising=False)
        monkeypatch.delenv("SPIRICAMERA_CAMERA_NAME", raising=False)
        widgets = "mu-1/hud/widgets/horizon, mu-1/robot_home_hud"
        monkeypatch.setenv("SPIRICAMERA_HUD_WIDGETS", widgets)
        made = self._topics(monkeypatch)
        assert set(made["cameras/1/hud"]["overlay_widgets"]) == {
            "mu-1/hud/widgets/horizon",
            "mu-1/robot_home_hud",
        }
        assert made["cameras/1/raw"]["overlay_widgets"] == {}
        assert made["cameras/1/preview"]["overlay_widgets"] == {}

    def test_camera_name_is_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SPIRICAMERA_CAMERA_NAME names the camera among the host's."""
        monkeypatch.delenv("SPIRICAMERA_TOPIC", raising=False)
        monkeypatch.setenv("SPIRICAMERA_CAMERA_NAME", "belly")
        made = self._topics(monkeypatch)
        assert set(made) == {"cameras/belly/raw", "cameras/belly/hud", "cameras/belly/preview"}

    def test_topic_overrides_the_whole_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SPIRICAMERA_TOPIC replaces cameras/<name>."""
        monkeypatch.setenv("SPIRICAMERA_TOPIC", "camera1")
        made = self._topics(monkeypatch)
        assert set(made) == {"camera1/raw", "camera1/hud", "camera1/preview"}


class TestValidate:
    """The validate command."""

    def test_describes_a_good_source(self) -> None:
        """Validation reports the resolved source and its capabilities."""
        result = runner.invoke(app, ["validate", "testimage://"])
        assert result.exit_code == 0
        assert "Scheme:       testimage" in result.stdout
        assert "Target:       pm5544" in result.stdout
        assert "Connected:    True" in result.stdout
        assert "Capture:" in result.stdout

    def test_rejects_a_bad_source(self) -> None:
        """An unresolvable source exits non-zero with the reason."""
        result = runner.invoke(app, ["validate", "nope://x"])
        assert result.exit_code == 1
        assert "Invalid source" in result.output

    def test_rejects_a_path_that_is_not_a_device(self) -> None:
        """A path that is neither a camera nor a file fails to resolve."""
        result = runner.invoke(app, ["validate", "/dev/video-does-not-exist"])
        assert result.exit_code == 1
        assert "Invalid source" in result.output

    def test_reports_a_device_that_will_not_open(self) -> None:
        """A resolvable but unopenable device is a failure, not a crash.

        The explicit scheme is taken at face value, so this gets as far
        as trying to open the device.
        """
        result = runner.invoke(app, ["validate", "v4l:///dev/video-does-not-exist"])
        assert result.exit_code == 1
        assert "Connected:    False" in result.output


class TestCapture:
    """The capture command."""

    def test_writes_frames_to_a_file(self, tmp_path: Path) -> None:
        """Requested frames are written out as image data."""
        output = tmp_path / "frames.jpg"

        result = runner.invoke(app, ["capture", "testimage://", "-f", "2", "-o", str(output)])

        assert result.exit_code == 0
        assert output.exists()
        assert output.read_bytes().startswith(b"\xff\xd8")

    def test_writes_to_stdout_by_default(self) -> None:
        """Without -o the frame goes to stdout for piping."""
        result = runner.invoke(app, ["capture", "testimage://", "-f", "1"])
        assert result.exit_code == 0

    def test_fails_on_an_unopenable_source(self, tmp_path: Path) -> None:
        """Capture exits non-zero rather than writing an empty file."""
        result = runner.invoke(
            app,
            [
                "capture",
                "v4l:///dev/video-does-not-exist",
                "-o",
                str(tmp_path / "x.jpg"),
            ],
        )
        assert result.exit_code == 1
