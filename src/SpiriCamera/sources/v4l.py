"""Local Video4Linux2 camera source."""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from SpiriCamera.sources.base import SourceError, SourceURL
from SpiriCamera.sources.capture import OpenCVSource

#: ``/dev/videoN`` and friends.
_DEVICE_PATH = re.compile(r"^/dev/")
#: Trailing index of a ``/dev/videoN`` node.
_DEVICE_INDEX = re.compile(r"^/dev/video(\d+)$")
#: Where Linux exposes V4L2 device identity.
_SYSFS_ROOT = Path("/sys/class/video4linux")


class V4LSource(OpenCVSource):
    """Local V4L2 camera.

    Accepts an explicit ``v4l://`` or ``v4l2://`` URL, a device path
    such as ``/dev/video0``, or a bare camera index such as ``0``.
    """

    schemes = ("v4l", "v4l2")

    @classmethod
    def handles(cls, url: SourceURL) -> bool:
        """Claim explicit V4L URLs, ``/dev`` paths, and bare indices.

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
        return bool(_DEVICE_PATH.match(url.target)) or url.target.isdigit()

    @classmethod
    def from_url(cls, url: SourceURL) -> V4LSource:
        """Validate the target before constructing the handler.

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
            If the target is neither a device path nor a camera index.
        """
        target = url.target
        if not target:
            raise SourceError(f"No V4L2 device given in {url.raw!r}")
        if not _DEVICE_PATH.match(target) and not target.isdigit():
            raise SourceError(
                f"Not a V4L2 device path or camera index: {target!r}"
            )
        return cls(url)

    def capture_target(self) -> str | int:
        """Return a camera index for bare digits, else the device path."""
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
        if self.target.isdigit():
            name = f"video{self.target}"
        elif match := _DEVICE_INDEX.match(self.target):
            name = f"video{match.group(1)}"
        else:
            # Symlinks such as /dev/v4l/by-id/... resolve to a real node.
            try:
                name = Path(self.target).resolve().name
            except OSError:
                return None

        node = _SYSFS_ROOT / name
        return node if node.is_dir() else None


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
