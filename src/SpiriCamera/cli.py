"""CLI interface for SpiriCamera."""

from __future__ import annotations

import contextlib
import sys
import time
from typing import Annotated

import typer
from loguru import logger
from SpiriSynq.example_types.position import Pose
from SpiriSynq.link import Link

from SpiriCamera import __version__
from SpiriCamera.camera import Camera, CameraError
from SpiriCamera.main import get_settings
from SpiriCamera.sources import SourceError, describe_source, known_schemes

app = typer.Typer(
    name="SpiriCamera",
    help="Work with Cameras in SpiriSynq",
    no_args_is_help=True,
    pretty_exceptions_short=True,
)

SourceArgument = Annotated[str, typer.Argument(help="Camera source")]

#: The preview camera's height in pixels, and its frames per second: a
#: thumbnail for picking a camera, cheap enough to leave subscribed.
PREVIEW_HEIGHT = 240
PREVIEW_FRAMERATE = 1


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


def _set_lens(cam: Camera) -> None:
    """Give the camera the lens its settings describe, or exit if it isn't one."""
    lens = get_settings().intrinsics()
    if lens is None:
        return
    try:
        cam.set_intrinsics("manual", **lens)
    except ValueError as exc:
        typer.echo(f"Error: SPIRICAMERA_ lens settings: {exc}", err=True)
        raise typer.Exit(1) from exc


def _set_mount(cam: Camera) -> None:
    """Mount the camera on what its settings name, or exit if that isn't a topic."""
    relative_to = get_settings().relative_to
    if not relative_to:
        return
    try:
        cam.relative_to, cam.mount = Link(relative_to), Pose()
    except ValueError as exc:
        typer.echo(f"Error: SPIRICAMERA_RELATIVE_TO: {exc}", err=True)
        raise typer.Exit(1) from exc


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
        raise typer.Exit(1) from exc

    typer.echo(f"Source:       {info.url}")
    typer.echo(f"Scheme:       {info.scheme}")
    typer.echo(f"Target:       {info.target}")
    typer.echo(f"Handler:      {info.handler}")

    cam = Camera(source=source)
    _set_lens(cam)
    try:
        cam.start(background=False)
    except CameraError as exc:
        typer.echo("Connected:    False")
        typer.echo(f"Error:        {exc}")
        raise typer.Exit(1) from exc

    typer.echo("Connected:    True")
    typer.echo(f"Capture:      {cam.describe_capabilities()}")
    if cam.vendor or cam.model:
        typer.echo(f"Device:       {cam.vendor} {cam.model}".strip())
    if cam.serial_number:
        typer.echo(f"Serial:       {cam.serial_number}")
    if cam.intrinsics is None:
        # A field of view becomes a lens at the first frame's size.
        with contextlib.suppress(CameraError):
            cam.read()
    lens = cam.intrinsics
    typer.echo(f"Lens:         {f'{lens} ({lens.origin})' if lens else 'unknown'}")
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

    base = settings.topic or f"cameras/{settings.camera_name}"
    cam = Camera(
        source=source,
        quality=settings.quality,
        max_width=settings.frame_width,
        max_height=settings.frame_height,
        max_framerate=settings.framerate,
        synq_topic=f"{base}/raw",
    )
    _set_lens(cam)
    _set_mount(cam)
    # Both relay the raw camera in this process, so each runs, and runs it,
    # only while something wants its own frames.
    upstream = f"spirisynq+raw://./{base}/raw"
    hud = Camera(
        source=upstream,
        quality=settings.quality,
        max_width=settings.frame_width,
        max_height=settings.frame_height,
        max_framerate=settings.framerate,
        synq_topic=f"{base}/hud",
    )
    # A new camera has no widgets, so the HUD's are named on it at each start.
    for widget in settings.hud_widgets.replace(",", " ").split():
        hud.overlay_widgets[widget] = {}
    preview = Camera(
        source=upstream,
        quality=settings.quality,
        # The height is what binds: raw is never wider than this.
        max_width=settings.frame_width,
        max_height=PREVIEW_HEIGHT,
        max_framerate=PREVIEW_FRAMERATE,
        synq_topic=f"{base}/preview",
    )
    cameras = (cam, hud, preview)

    try:
        for each in cameras:
            each.start()
    except CameraError as exc:
        typer.echo(f"Error: {exc}", err=True)
        for each in cameras:
            each.stop()
        raise typer.Exit(1) from exc

    rtsp = None
    if settings.rtsp_port:
        from SpiriCamera.rtsp import RtspServer

        # In this process, so its readers share the camera's one encode.
        rtsp = RtspServer(
            {"raw": cam, "hud": hud, "preview": preview},
            port=settings.rtsp_port,
            udp_port=settings.rtsp_udp_port,
        )
        try:
            rtsp.start()
        except OSError as exc:
            typer.echo(f"Error: can't serve RTSP: {exc}", err=True)
            for each in cameras:
                each.stop()
            raise typer.Exit(1) from exc
        logger.info(
            f"Serving /raw, /hud and /preview over RTSP on :{settings.rtsp_port}, "
            f"UDP from :{settings.rtsp_udp_port}"
        )

    logger.info(
        f"Publishing on {', '.join(each.synq_topic for each in cameras)}, press Ctrl-C to stop"
    )
    try:
        # The camera runs on its own thread, when something wants frames.
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if rtsp is not None:
            rtsp.stop()
        # Relays first, so they don't wait on a raw camera that's gone.
        for each in reversed(cameras):
            each.stop()
        logger.info("Camera stopped")


@app.command()
def capture(
    source: SourceArgument = "",
    frames: Annotated[int, typer.Option("-f", "--frames", help="Number of frames to capture")] = 1,
    quality: Annotated[int, typer.Option("-q", "--quality", help="JPEG quality")] = 80,
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
        raise typer.Exit(1) from exc

    captured = 0
    # Not a `with`: it may be stdout, which isn't ours to close. Closed in `finally`.
    handle = open(output, "wb") if output else sys.stdout.buffer  # noqa: SIM115
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
