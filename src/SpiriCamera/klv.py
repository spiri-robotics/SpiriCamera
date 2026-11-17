"""KLV metadata for a camera's frames: a MISB ST 0601 UAS Datalink Local Set.

:py:mod:`SpiriCamera.rtsp` sends one with each frame, at that frame's timestamp,
and MediaMTX carries them on into MPEG-TS for STANAG 4609 consumers.
"""

from __future__ import annotations

import struct

#: The Universal Label that identifies a UAS Datalink Local Set.
UAS_LOCAL_SET_KEY = bytes.fromhex("060E2B34020B01010E01030101000000")

#: The version of ST 0601 these sets declare, in tag 65.
ST0601_VERSION = 19

_TAG_CHECKSUM = 1
_TAG_PRECISION_TIMESTAMP = 2
_TAG_VERSION = 65


def _ber_length(length: int) -> bytes:
    """A BER length: one byte below 128, otherwise a byte count and the bytes."""
    if length < 128:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _checksum(data: bytes) -> int:
    """ST 0601's checksum: a 16-bit sum, with bytes alternately the high and low half."""
    total = 0
    for index, byte in enumerate(data):
        total += byte << (8 * ((index + 1) % 2))
    return total & 0xFFFF


def uas_local_set(timestamp: float) -> bytes:
    """Build the local set for one frame.

    Parameters
    ----------
    timestamp : float
        When the frame was captured, in seconds since the UNIX epoch.

    Returns
    -------
    bytes
        A complete KLV packet: key, BER length and items, ending in the checksum.
    """
    items = (
        bytes([_TAG_PRECISION_TIMESTAMP, 8])
        + struct.pack(">Q", round(timestamp * 1_000_000))
        + bytes([_TAG_VERSION, 1, ST0601_VERSION])
    )
    # The checksum covers everything up to its own value, so its tag and length too.
    checksum_header = bytes([_TAG_CHECKSUM, 2])
    length = len(items) + len(checksum_header) + 2
    covered = UAS_LOCAL_SET_KEY + _ber_length(length) + items + checksum_header
    return covered + struct.pack(">H", _checksum(covered))


__all__ = ["ST0601_VERSION", "UAS_LOCAL_SET_KEY", "uas_local_set"]
