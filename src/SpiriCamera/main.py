"""Main entry point for SpiriCamera."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict

from SpiriCamera import __version__


class Settings(BaseSettings):
    """Settings for SpiriCamera, loaded from environment variables.

    Environment variables are prefixed with ``SPIRICAMERA_``.  Example::

        export SPIRICAMERA_SOURCE="rtsp://admin:pass@192.168.1.100:554/stream"
        export SPIRICAMERA_QUALITY=85
        export SPIRICAMERA_FRAME_WIDTH=1280
        export SPIRICAMERA_FRAME_HEIGHT=720
        export SPIRICAMERA_FRAMERATE=30

    Attributes
    ----------
    app_name : str
        Application name displayed in logs.
    version : str
        Current package version.
    environment : str
        Runtime environment (``development``, ``production``, etc.).
    debug : bool
        Enable verbose debugging output.
    log_level : str
        Logging level (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``).
    source : str
        Camera source string (e.g. ``/dev/video0``, ``rtsp://...``).
    quality : int
        JPEG encoding quality for captured frames [1-100].
    frame_width : int
        User-requested capture width (default 1920).
    frame_height : int
        User-requested capture height (default 1080).
    framerate : int
        User-requested capture framerate (default 30).
    """

    model_config = SettingsConfigDict(
        env_prefix="SPIRICAMERA_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "SpiriCamera"
    version: str = __version__
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    # Camera configuration
    source: str = ""
    quality: int = 80
    frame_width: int = 1920
    frame_height: int = 1080
    framerate: int = 30


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru logging.

    Parameters
    ----------
    level : str
        Logging level string (e.g. ``DEBUG``, ``INFO``).
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )


def get_settings() -> Settings:
    """Load settings from environment.

    Returns
    -------
    Settings
        Configured settings instance.
    """
    return Settings()


def main() -> None:
    """Main entry point."""
    settings = get_settings()
    setup_logging(level="DEBUG" if settings.debug else settings.log_level)

    logger.info(
        "Starting {} v{} [environment: {}]",
        settings.app_name,
        settings.version,
        settings.environment,
    )

    # Add your main application logic here
    logger.info("{} is running", settings.app_name)


if __name__ == "__main__":
    main()
