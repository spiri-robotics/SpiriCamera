"""Tests for SpiriCamera CLI."""

from __future__ import annotations

from SpiriCamera.cli import app


def test_cli_version() -> None:
    """Test CLI version command."""

    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0


def test_cli_help() -> None:
    """Test CLI help command."""
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "SpiriCamera" in result.stdout

