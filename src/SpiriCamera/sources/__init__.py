"""Camera source handlers and discovery.

This module provides the SourceBase ABC and a :py:func:`discover` function
that walks all subclasses to find the appropriate handler for a given
source string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from SpiriCamera.camera import CameraSource
# Import all source handlers to register them (side effect)
# flake8: noqa: F401
from SpiriCamera.sources.base import (  # noqa: E402
    SourceRegistrationError,
    SourceBase,
)

from . import v4l, network, testimage  # noqa: E402

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Source registry - populated on first access
# ---------------------------------------------------------------------------

_registry_cache: list[tuple[type[SourceBase], str]] = []


def _load_registry() -> list[tuple[type[SourceBase], str]]:
    """Load all registered source handlers.

    Recursively enumerates subclasses of SourceBase and pairs each
    class with its supported protocol prefixes.

    Returns
    -------
    list[tuple[type[SourceBase], str]]
        Pairs of (source_class, protocol_prefix).
    """
    global _registry_cache
    if not _registry_cache:
        _registry_cache = _discover_sources(SourceBase)
    return _registry_cache


def _discover_sources(cls: type) -> list[tuple[type[SourceBase], str]]:
    """Recursively discover all SourceBase subclasses and their protocols.

    Parameters
    ----------
    cls : type
        The base class to walk.

    Returns
    -------
    list[tuple[type[SourceBase], str]]
        Pairs of (subclass, protocol_prefix).
    """
    result: list[tuple[type[SourceBase], str]] = []
    for subclass in cls.__subclasses__():
        result.extend(_discover_sources(subclass))
        if not hasattr(subclass, "protocols") or not subclass.protocols:
            continue
        for protocol in subclass.protocols:
            result.append((subclass, protocol))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover(source_str: str) -> tuple[CameraSource, SourceBase]:
    """Discover the appropriate source handler for a source string.

    Walks all registered source classes (via `cls.__subclasses__()`)
    and matches the source string against their `protocols` tuples.
    Falls back to :py:class:`~SpiriCamera.sources.v4l.V4LSource` for
    V4L2 device paths or bare numeric indices.

    Parameters
    ----------
    source_str : str
        The source string to parse (e.g. ``"/dev/video0"``,
        ``"rtsp://host/stream"``, ``"testimage://pm5544"``).

    Returns
    -------
    tuple[CameraSource, SourceBase]
        A ``(parsed_dataclass, source_handler)`` pair.  The dataclass
        contains parsed scheme/path/type fields; the handler is the
        concrete source instance ready for :py:meth:`SourceBase.start`.

    Raises
    ------
    ValueError
        If the source string is empty or unrecognised.
    """
    if not source_str or not source_str.strip():
        raise ValueError("Source string cannot be empty")

    source_str = source_str.strip()

    # Check exact protocol prefixes first
    for handler_cls, protocol in _load_registry():
        if source_str.startswith(protocol):
            parsed = _parse_for_protocol(source_str, protocol, handler_cls)
            # TestImageSource uses image_name, not path
            if protocol == "testimage://":
                image_name_val = parsed.get("image_name", "pm5544")
                path_val = image_name_val
            else:
                image_name_val = None
                path_val = parsed.get("path", source_str)
                # Ensure path is in handler kwargs if not present
                if "path" not in parsed:
                    parsed["path"] = path_val
            source_instance = handler_cls(**parsed)
            # Determine source_type from protocol
            if protocol in ("rtsp://", "rtmp://", "http://", "https://"):
                source_type = "network"
            else:
                source_type = "local"
            return CameraSource(
                source_type=source_type,
                scheme=parsed.get("scheme", protocol.rstrip("://")),
                path=path_val,
                camera_index=parsed.get("camera_index", None),
                is_valid=True,
                source=source_str,
            ), source_instance

    # Check V4L2 patterns (always fallback)
    v4l_m = re.match(r"^/dev/video(\d+)$", source_str)
    if v4l_m:
        idx = int(v4l_m.group(1))
        return CameraSource(
            source_type="local",
            scheme="v4l",
            path=source_str,
            camera_index=idx,
            is_valid=True,
            source=source_str,
        ), _make_source_for_scheme("v4l", source_str, idx)

    # Check bare numeric index
    if source_str.isdigit() and int(source_str) >= 0:
        idx = int(source_str)
        return CameraSource(
            source_type="local",
            scheme="v4l",
            path=str(idx),
            camera_index=idx,
            is_valid=True,
            source=source_str,
        ), _make_source_for_scheme("v4l", str(idx), idx)

    raise ValueError(
        f"Unrecognised source: {source_str!r}. "
        "Supported formats: /dev/videoN, numeric index, rtsp://, rtmp://, "
        "http://, https://, testimage:// "
    )


def _parse_for_protocol(source_str: str, protocol: str, handler_cls: type) -> dict[str, Any]:
    """Parse a source string for a specific protocol.

    Parameters
    ----------
    source_str : str
        The full source string.
    protocol : str
        The matched protocol prefix.
    handler_cls : type
        The source handler class.

    Returns
    -------
    dict[str, Any]
        Keyword arguments for the handler constructor.
    """
    if protocol == "testimage://":
        image_name = source_str.replace("testimage://", "").strip()
        if not image_name:
            image_name = "pm5544"
        return {"image_name": image_name}

    if protocol in ("rtsp://", "rtmp://", "http://", "https://"):
        scheme = protocol.rstrip("://")
        return {"path": source_str, "scheme": scheme}

    # Default fallback
    return {"path": source_str}


def _make_source_for_scheme(scheme: str, path: str, camera_index: int) -> SourceBase:
    """Create the appropriate source instance for a scheme.

    Parameters
    ----------
    scheme : str
        The scheme name (e.g. 'v4l', 'rtsp').
    path : str
        The path or URL.
    camera_index : int
        The V4L2 camera index (if applicable).

    Returns
    -------
    SourceBase
        An instance of the appropriate source handler.
    """
    if scheme == "v4l":
        from SpiriCamera.sources.v4l import V4LSource  # noqa: PLC0415
        return V4LSource(path=path, camera_index=camera_index)

    raise ValueError(f"Unknown scheme: {scheme!r}")


def validate_source(source_str: str) -> CameraSource:
    """Validate a source string by discovering its handler.

    This is a convenience wrapper around :py:func:`discover` that
    returns only the :py:class:`CameraSource` dataclass without
    instantiating the actual source handler.

    Parameters
    ----------
    source_str : str
        The source string to validate.

    Returns
    -------
    CameraSource
        The parsed representation.

    Raises
    ------
    ValueError
        If the source string is not valid.
    """
    info, _ = discover(source_str)
    return info


__all__ = [
    "SourceBase",
    "SourceRegistrationError",
    "discover",
    "validate_source",
]
