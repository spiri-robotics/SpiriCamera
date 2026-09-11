"""CLI interface for SpiriCamera."""

from __future__ import annotations

import sys
from typing import Annotated

import typer
from loguru import logger

from SpiriCamera import __version__
from SpiriCamera.camera import Camera
from SpiriCamera.main import Settings, get_settings

app = typer.Typer(
    name="SpiriCamera",
    help="Work with Cameras in SpiriSynq",
    no_args_is_help=True,
    pretty_exceptions_short=True,
)


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

    log_level = "DEBUG" if verbose else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=log_level)


@app.command()
def version() -> None:
    """Show version information."""
    typer.echo(f"SpiriCamera v0.1.0")
    typer.echo(f"Python {sys.version.split()[0]}")


@app.command()
def validate(
    source: Annotated[str, typer.Argument(help="Camera source to validate")] = "",
) -> None:
    """Validate a camera source without starting capture.

    Parses the source, attempts to open the capture device, reports
    its capabilities, and exits.
    """
    # Load settings, allow CLI source to override env var
    settings = get_settings()
    if not source:
        source = settings.source

    if not source:
        typer.echo("Error: No source specified. Use --source or set SPIRICAMERA_SOURCE.", err=True)
        raise typer.Exit(1)

    try:
        source_obj = Camera.validate_source(source)
    except ValueError as e:
        typer.echo(f"Invalid source: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Source type:  {source_obj.source_type}")
    typer.echo(f"Scheme:       {source_obj.scheme}")
    typer.echo(f"Path:         {source_obj.path}")
    typer.echo(f"Camera index: {source_obj.camera_index}")
    typer.echo(f"Valid:        {source_obj.is_valid}")

    # Attempt to open and read capabilities
    cam = Camera(source=source)
    try:
        cam.start()
        typer.echo(f"Resolution:   {cam.max_supported_width}x{cam.max_supported_height}")
        typer.echo(f"Framerate:    {cam.max_supported_framerate} fps")
        typer.echo(f"Connected:    {cam.running}")
    except RuntimeError as e:
        typer.echo(f"Connected:    False")
        typer.echo(f"Error:        {e}")

    cam.stop()
    typer.echo("Source validation complete.")


@app.command()
def run(
    source: Annotated[str, typer.Argument(help="Camera source to capture from")] = "",
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Run SpiriCamera with source from environment or argument."""
    settings = get_settings()
    if not verbose:
        logger.remove()
        logger.add(sys.stderr, level="INFO")

    # Merge CLI source with env var
    if not source:
        source = settings.source

    if not source:
        typer.echo("Error: No source specified. Use --source or set SPIRICAMERA_SOURCE.", err=True)
        raise typer.Exit(1)

    logger.info("Starting camera: %s | quality=%d | %dx%d @ %dfps",
                 source, settings.quality, settings.frame_width, settings.frame_height, settings.framerate)

    cam = Camera(
        source=source,
        quality=settings.quality,
        max_width=settings.frame_width,
        max_height=settings.frame_height,
        max_framerate=settings.framerate,
    )

    frame_count = 0
    try:
        cam.start()
        logger.info("Camera started: %s", cam.synq_topic)

        # Read frames in a loop
        while cam.running:
            try:
                cam.read()
                frame_count += 1
            except RuntimeError as e:
                logger.error("Frame read failed: %s", e)

    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()
        logger.info("Camera stopped")


@app.command()
def capture(
    source: Annotated[str, typer.Argument(help="Camera source to capture from")] = "",
    frames: Annotated[int, typer.Option("-f", "--frames", help="Number of frames to capture")] = 1,
    quality: Annotated[int, typer.Option("-q", "--quality", help="JPEG quality")] = 80,
    output: Annotated[str, typer.Option("-o", "--output", help="Output filename (default: stdout)")] = "",
) -> None:
    """Capture a specific number of frames from a camera source.

    Writes JPEG frames to the specified output file, or to stdout
    if no output path is given.
    """
    settings = get_settings()
    if not source:
        source = settings.source

    if not source:
        typer.echo("Error: No source specified. Use --source or set SPIRICAMERA_SOURCE.", err=True)
        raise typer.Exit(1)

    final_quality = quality if quality else settings.quality

    cam = Camera(source=source, quality=final_quality)

    captured = 0
    try:
        cam.start()

        # Open output file or use stdout
        fh = None
        if output:
            fh = open(output, "wb")
            logger.info("Writing %d frames to file: %s", frames, output)
        else:
            fh = sys.stdout.buffer
            logger.info("Writing %d frames to stdout", frames)

        while captured < frames and cam.running:
            try:
                data = cam.read()
                fh.write(data)
                captured += 1
            except RuntimeError as e:
                logger.error("Frame read failed: %s", e)
                break

        if fh and fh is not sys.stdout.buffer:
            fh.close()
        logger.info("Captured %d frames", captured)

    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()
        logger.info("Camera stopped")


if __name__ == "__main__":
    app()
