"""CLI interface for SpiriCamera."""

from __future__ import annotations

import sys
import time
from typing import Annotated

import typer
from loguru import logger

from SpiriCamera import __version__
from SpiriCamera.camera import Camera, CameraError
from SpiriCamera.sources import SourceError, describe_source, known_schemes
from SpiriCamera.main import get_settings

app = typer.Typer(
    name="SpiriCamera",
    help="Work with Cameras in SpiriSynq",
    no_args_is_help=True,
    pretty_exceptions_short=True,
)

SourceArgument = Annotated[str, typer.Argument(help="Camera source")]


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable verbose logging")
    ] = False,
):
    """Enter callback for SpiriCamera."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        ctx.exit()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")


def _resolve_source(source: str) -> str:
    """Fall back to the configured source, or exit with a usage error."""
    source = source or get_settings().source
    if not source:
        typer.echo(
            "Error: no source given. Pass one as an argument or set "
            f"SPIRICAMERA_SOURCE. Supported: {', '.join(known_schemes())}, "
            "/dev/videoN, or a bare camera index.",
            err=True,
        )
        raise typer.Exit(1)
    return source


@app.command()
def version() -> None:
    """Show version information."""
    typer.echo(f"SpiriCamera v{__version__}")
    typer.echo(f"Python {sys.version.split()[0]}")


@app.command()
def sources() -> None:
    """List the registered source handlers and the schemes they claim."""
    from SpiriCamera.sources import registered_sources

    for handler in registered_sources():
        schemes = ", ".join(f"{scheme}://" for scheme in handler.schemes) or "-"
        typer.echo(f"{handler.__name__:<18} {schemes}")


@app.command()
def validate(source: SourceArgument = "") -> None:
    """Validate a camera source, then report what the device can do."""
    source = _resolve_source(source)

    try:
        info = describe_source(source)
    except SourceError as exc:
        typer.echo(f"Invalid source: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Source:       {info.url}")
    typer.echo(f"Scheme:       {info.scheme}")
    typer.echo(f"Target:       {info.target}")
    typer.echo(f"Handler:      {info.handler}")

    cam = Camera(source=source)
    try:
        cam.start(background=False)
    except CameraError as exc:
        typer.echo(f"Connected:    False")
        typer.echo(f"Error:        {exc}")
        raise typer.Exit(1)

    typer.echo(f"Connected:    True")
    typer.echo(f"Capture:      {cam.describe_capabilities()}")
    if cam.vendor or cam.model:
        typer.echo(f"Device:       {cam.vendor} {cam.model}".strip())
    if cam.serial_number:
        typer.echo(f"Serial:       {cam.serial_number}")
    cam.stop()


@app.command()
def run(source: SourceArgument = "") -> None:
    """Capture continuously and publish frames to SpiriSynq until interrupted."""
    source = _resolve_source(source)
    settings = get_settings()

    logger.info(
        f"Starting camera: {source} | quality={settings.quality} | "
        f"{settings.frame_width}x{settings.frame_height} @ {settings.framerate}fps"
    )

    cam = Camera(
        source=source,
        quality=settings.quality,
        max_width=settings.frame_width,
        max_height=settings.frame_height,
        max_framerate=settings.framerate,
    )

    try:
        cam.start()
    except CameraError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    logger.info(f"Publishing on {cam.synq_topic}, press Ctrl-C to stop")
    try:
        # The camera captures on its own thread; just stay alive.
        while cam.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()
        logger.info("Camera stopped")


@app.command()
def capture(
    source: SourceArgument = "",
    frames: Annotated[
        int, typer.Option("-f", "--frames", help="Number of frames to capture")
    ] = 1,
    quality: Annotated[
        int, typer.Option("-q", "--quality", help="JPEG quality")
    ] = 80,
    output: Annotated[
        str,
        typer.Option("-o", "--output", help="Output file (default: stdout)"),
    ] = "",
) -> None:
    """Capture a fixed number of frames and write them out."""
    source = _resolve_source(source)
    settings = get_settings()

    cam = Camera(
        source=source,
        quality=quality or settings.quality,
        max_width=settings.frame_width,
        max_height=settings.frame_height,
        max_framerate=settings.framerate,
    )

    try:
        # No capture thread: this command drives the frames itself.
        cam.start(background=False)
    except CameraError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    captured = 0
    handle = open(output, "wb") if output else sys.stdout.buffer
    logger.info(f"Writing {frames} frame(s) to {output or 'stdout'}")

    try:
        while captured < frames:
            try:
                handle.write(cam.read())
                captured += 1
            except CameraError as exc:
                logger.error(f"Frame capture failed: {exc}")
                break
        handle.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if output:
            handle.close()
        cam.stop()

    logger.info(f"Captured {captured} frame(s)")
    if captured < frames:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
