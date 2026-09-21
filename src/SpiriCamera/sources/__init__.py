"""Source handler registry and resolution.

Handlers register themselves simply by being imported: the registry
walks :py:class:`~SpiriCamera.sources.base.SourceBase` subclasses and
asks each one whether it recognises a given source string.  Adding a
new protocol means adding a module here — no dispatch table to update,
and no protocol-specific knowledge anywhere but the handler itself.
"""

from __future__ import annotations

import inspect

from SpiriCamera.sources.base import (
    CaptureSettings,
    SourceBase,
    SourceCapabilities,
    SourceError,
    SourceInfo,
    SourceURL,
    UnknownSourceError,
)
from SpiriCamera.sources.capture import OpenCVSource

# Importing these modules is what registers their handlers.
from SpiriCamera.sources import file, network, testimage, v4l, webrtc  # noqa: F401


def registered_sources() -> list[type[SourceBase]]:
    """List every concrete source handler currently importable.

    Returns
    -------
    list[type[SourceBase]]
        Concrete :py:class:`SourceBase` subclasses, depth-first.
    """
    found: list[type[SourceBase]] = []
    seen: set[type[SourceBase]] = set()

    def walk(cls: type[SourceBase]) -> None:
        for subclass in cls.__subclasses__():
            if subclass in seen:
                continue
            seen.add(subclass)
            if not inspect.isabstract(subclass):
                found.append(subclass)
            walk(subclass)

    walk(SourceBase)
    return found


def resolve_source(source_str: str) -> SourceBase:
    """Find and instantiate the handler for a source string.

    Handlers that claim the URL's explicit scheme win over handlers that
    match schemeless strings, so ``v4l://0`` and ``/dev/video0`` both
    land on the V4L2 handler without either rule shadowing the other.

    Parameters
    ----------
    source_str : str
        Source string such as ``/dev/video0``, ``rtsp://host/stream``,
        or ``testimage://pm5544``.

    Returns
    -------
    SourceBase
        A handler instance, not yet opened.

    Raises
    ------
    SourceError
        If the source string is empty or malformed for its scheme.
    UnknownSourceError
        If no registered handler recognises the source string.
    """
    url = SourceURL.parse(source_str)

    by_scheme: list[type[SourceBase]] = []
    by_pattern: list[type[SourceBase]] = []
    for handler in registered_sources():
        if not handler.handles(url):
            continue
        if url.scheme and url.scheme in handler.schemes:
            by_scheme.append(handler)
        else:
            by_pattern.append(handler)

    for handler in (*by_scheme, *by_pattern):
        return handler.from_url(url)

    raise UnknownSourceError(
        f"Unrecognised source: {source_str!r}. Supported: "
        f"{', '.join(known_schemes())}, a camera index, a video device, "
        "or the path of an existing file"
    )


def describe_source(source_str: str) -> SourceInfo:
    """Resolve a source string and describe it without opening anything.

    Parameters
    ----------
    source_str : str
        The source string to inspect.

    Returns
    -------
    SourceInfo
        A network-transparent description of the resolved source.

    Raises
    ------
    SourceError
        If the source string cannot be resolved.
    """
    return resolve_source(source_str).describe()


def known_schemes() -> list[str]:
    """List the URL schemes the registered handlers claim.

    Returns
    -------
    list[str]
        Sorted scheme names, formatted as ``scheme://``.
    """
    schemes = {
        f"{scheme}://" for handler in registered_sources() for scheme in handler.schemes
    }
    return sorted(schemes)


__all__ = [
    "CaptureSettings",
    "OpenCVSource",
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
