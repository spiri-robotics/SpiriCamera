"""Local Video4Linux2 camera source."""

from __future__ import annotations

import stat
from pathlib import Path

from loguru import logger

from SpiriCamera.sources.base import SourceError, SourceURL
from SpiriCamera.sources.capture import OpenCVSource

#: Where Linux exposes V4L2 device identity.
_SYSFS_ROOT = Path("/sys/class/video4linux")


class V4LSource(OpenCVSource):
    """Local V4L2 camera.

    Accepts a bare camera index (``0``), a device node (``/dev/video0``,
    or a ``/dev/v4l/by-id/...`` symlink to one), or an explicit
    ``v4l://`` or ``v4l2://`` URL.

    A schemeless path is claimed by asking the filesystem whether it is
    a character device, rather than by matching its spelling.  That is
    what distinguishes a camera from a video file, and it means a
    half-typed path is reported as unrecognised instead of being claimed
    and then failing to open.  The explicit scheme skips the check, so a
    device that is not plugged in yet can still be configured.
    """

    schemes = ("v4l", "v4l2")

    @classmethod
    def handles(cls, url: SourceURL) -> bool:
        """Claim explicit V4L URLs, camera indices, and device nodes.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.

        Returns
        -------
        bool
            True if this is a local video device.
        """
        if url.scheme in cls.schemes:
            return True
        if url.scheme:
            return False
        return url.target.isdigit() or _is_video_device(url.target)

    @classmethod
    def from_url(cls, url: SourceURL) -> V4LSource:
        """Validate the target before constructing the handler.

        An explicit ``v4l://`` URL is taken at face value: the caller has
        said what they mean, and the device may appear later.  A
        schemeless target has to prove itself, since it was claimed by
        inspection rather than by intent.

        Parameters
        ----------
        url : SourceURL
            The parsed source string.

        Returns
        -------
        V4LSource
            A handler for the device.

        Raises
        ------
        SourceError
            If no device was given, or a schemeless target is neither a
            camera index nor a character device.
        """
        if not url.target:
            raise SourceError(f"No V4L2 device given in {url.raw!r}")

        if not url.scheme and not url.target.isdigit():
            if not _is_video_device(url.target):
                raise SourceError(f"Not a video device: {url.target!r}")
        return cls(url)

    def capture_target(self) -> str | int:
        """Return a camera index for bare digits, else the device path.

        A bare index must reach OpenCV as an ``int``; passed as a string
        it is treated as a filename.
        """
        if self.target.isdigit():
            return int(self.target)
        return self.target

    def device_metadata(self) -> dict[str, str]:
        """Read device identity out of sysfs.

        Returns
        -------
        dict[str, str]
            Vendor, model, and serial number, as far as sysfs reports
            them.  Empty on non-Linux hosts or unreadable devices.
        """
        node = self._sysfs_node()
        if node is None:
            return {}

        metadata: dict[str, str] = {}
        if model := _read_text(node / "name"):
            metadata["model"] = model

        usb = _usb_device_dir(node)
        if usb is not None:
            if vendor := _read_text(usb / "manufacturer"):
                metadata["vendor"] = vendor
            if product := _read_text(usb / "product"):
                metadata["model"] = product
            if serial := _read_text(usb / "serial"):
                metadata["serial_number"] = serial

        logger.debug(f"{self!r} sysfs metadata: {metadata}")
        return metadata

    def _sysfs_node(self) -> Path | None:
        """Locate this device's ``/sys/class/video4linux`` entry."""
        return _sysfs_node_for(self.target)


def list_devices() -> list[dict[str, str]]:
    """Enumerate local V4L2 camera devices.

    Existence in ``/sys/class/video4linux`` is what is trusted here, the
    same test :py:func:`_is_video_device` uses for a schemeless source
    string -- a device that has not been plugged in, or that sysfs is
    not mounted to describe, simply does not appear.

    Returns
    -------
    list[dict[str, str]]
        One entry per usable device node, each with ``path`` (a
        ``/dev/videoN`` node, usable directly as a source string) and
        ``label`` (its USB identity where sysfs reports one, else just
        the device node).
    """
    if not _SYSFS_ROOT.is_dir():
        return []

    devices = []
    for node in sorted(_SYSFS_ROOT.glob("video*"), key=lambda p: p.name):
        path = f"/dev/{node.name}"
        if not _is_character_device(path):
            continue
        metadata = V4LSource(SourceURL.parse(path)).device_metadata()
        identity = " ".join(
            filter(None, (metadata.get("vendor"), metadata.get("model")))
        )
        devices.append(
            {"path": path, "label": f"{identity} ({path})" if identity else path}
        )
    return devices


def _sysfs_node_for(target: str) -> Path | None:
    """Find the sysfs entry for a camera index or device path.

    Symlinks such as ``/dev/v4l/by-id/...`` are resolved first, so the
    node is found by what it points at rather than how it was spelled.

    Parameters
    ----------
    target : str
        A camera index or device path.

    Returns
    -------
    Path | None
        The device's sysfs directory, or ``None`` if it has none.
    """
    if target.isdigit():
        name = f"video{target}"
    else:
        try:
            name = Path(target).resolve().name
        except OSError:
            return None

    node = _SYSFS_ROOT / name
    return node if node.is_dir() else None


def _is_video_device(path: str) -> bool:
    """Whether a path is a character device Linux knows as a camera.

    The character-device check alone would also claim ``/dev/null`` and
    friends, so it is confirmed against sysfs.  Where sysfs is not
    mounted at all the device node is trusted on its own, since
    consulting it is then impossible rather than merely unhelpful.

    Parameters
    ----------
    path : str
        The path to inspect.

    Returns
    -------
    bool
        True if the path is a usable video device node.
    """
    if not _is_character_device(path):
        return False
    if not _SYSFS_ROOT.is_dir():
        return True
    return _sysfs_node_for(path) is not None


def _is_character_device(path: str) -> bool:
    """Whether a path resolves to a character device, without raising."""
    if not path:
        return False
    try:
        return stat.S_ISCHR(Path(path).stat().st_mode)
    except (OSError, ValueError):
        return False


def _read_text(path: Path) -> str:
    """Read a sysfs attribute, returning ``""`` when unavailable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _usb_device_dir(node: Path) -> Path | None:
    """Walk up from a V4L2 sysfs node to its owning USB device, if any."""
    try:
        current = (node / "device").resolve()
    except OSError:
        return None

    for candidate in (current, *current.parents):
        if (candidate / "idVendor").exists():
            return candidate
    return None
