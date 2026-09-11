"""SpiriCamera — Work with Cameras in SpiriSynq."""

from __future__ import annotations

from SpiriCamera.camera import Camera, CameraBase, CameraSource
from SpiriCamera.sources import discover, validate_source
from SpiriCamera.sources.base import SourceBase

__version__ = "0.1.0"

__all__ = [
    "Camera",
    "CameraBase",
    "CameraSource",
    "SourceBase",
    "discover",
    "validate_source",
]
