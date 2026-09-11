"""SpiriCamera — Work with Cameras in SpiriSynq."""

from __future__ import annotations

from SpiriCamera.camera import (
    Camera,
    CameraBase,
    CameraError,
    CameraNotStartedError,
    FrameUnavailableError,
)
from SpiriCamera.sources import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceInfo,
    SourceURL,
    UnknownSourceError,
    describe_source,
    known_schemes,
    registered_sources,
    resolve_source,
)

__version__ = "0.1.0"

__all__ = [
    "Camera",
    "CameraBase",
    "CameraError",
    "CameraNotStartedError",
    "CaptureSettings",
    "FrameUnavailableError",
    "SourceBase",
    "SourceCapabilities",
    "SourceError",
    "SourceInfo",
    "SourceURL",
    "UnknownSourceError",
    "describe_source",
    "known_schemes",
    "registered_sources",
    "resolve_source",
]
