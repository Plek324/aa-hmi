"""video_protocol.py -- wire framing for the TCP/TLS video session.

Distinct from protocol.py's RFCOMM framing (2-byte length + 2-byte msg_id).
This is the TCP-side framing used on port 29880: every frame -- before AND
after TLS is established -- starts with a 4-byte header (channel:u8,
flags:u8, length:u16, big-endian), then `length` bytes of body. Pre-TLS,
body = 2-byte message_id + plaintext payload. Post-TLS, body = one raw TLS
record (the message_id lives inside the decrypted plaintext instead).

Reuses protocol.py's varint/string helpers for the plaintext protobuf
bodies exchanged before/during the handshake and for control messages --
no need to duplicate those, they're transport-agnostic.
"""
from __future__ import annotations

import socket
import struct

from .errors import VideoSessionError

WIRE_HEADER_LEN = 4  # channel:u8, flags:u8, length:u16


class WireFrameError(VideoSessionError):
    """Malformed TCP-side wire frame (bad header, truncated payload)."""


def make_wire_frame(channel: int, flags: int, body: bytes) -> bytes:
    return struct.pack(">BBH", channel, flags, len(body)) + body


def read_wire_frame(sock: socket.socket, timeout: float | None = None) -> tuple[int, int, bytes] | None:
    """Read one complete wire frame: (channel, flags, body), or None on a
    clean EOF before any bytes of a new frame arrive."""
    if timeout is not None:
        sock.settimeout(timeout)
    header = _read_exact(sock, WIRE_HEADER_LEN, allow_empty=True)
    if header is None:
        return None
    channel, flags, length = struct.unpack(">BBH", header)
    body = _read_exact(sock, length, allow_empty=False)
    if body is None:
        raise WireFrameError(f"connection closed mid-body (channel={channel}, wanted {length} bytes)")
    return channel, flags, body


def _read_exact(sock: socket.socket, n: int, *, allow_empty: bool) -> bytes | None:
    if n == 0:
        return b""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if allow_empty and not buf:
                return None
            raise WireFrameError(f"connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


# --- multi-frame messages ---
#
# The flags byte, per aasdk's FrameHeader: bits 0-1 = frame type
# (MIDDLE=0, FIRST=1, LAST=2, BULK=3 i.e. "whole message in one frame"),
# bit 2 = control message on a non-control channel, bit 3 = encrypted.
# 0x0B = encrypted BULK. A message whose plaintext exceeds one frame's
# 16384-byte payload limit goes out as FIRST, MIDDLE..., LAST; the FIRST
# frame's header carries an extra u32 with the total plaintext size.
#
# Replaces aa_pi2display's split_tls_records, which sent each TLS record
# of an oversized message as a separate BULK frame -- so the display saw
# every piece as a complete (garbage) message of its own, the likely real
# reason "a whole frame as one big message" gave a black screen there.
# Not yet verified on hardware (clock frames stay under 16KB); layout
# taken from aasdk.

MAX_FRAME_PAYLOAD = 16384
FRAME_TYPE_MASK = 0x03
FRAME_MIDDLE, FRAME_FIRST, FRAME_LAST, FRAME_BULK = 0, 1, 2, 3


def split_plaintext(plaintext: bytes, limit: int = MAX_FRAME_PAYLOAD) -> list[bytes]:
    """Chunks of at most `limit` bytes; one chunk if it already fits."""
    if len(plaintext) <= limit:
        return [plaintext]
    return [plaintext[i:i + limit] for i in range(0, len(plaintext), limit)]


def fragment_flags(flags: int, index: int, count: int) -> int:
    """Frame-type bits for fragment `index` of `count` (BULK if count==1)."""
    base = flags & ~FRAME_TYPE_MASK
    if count == 1:
        return base | FRAME_BULK
    if index == 0:
        return base | FRAME_FIRST
    if index == count - 1:
        return base | FRAME_LAST
    return base | FRAME_MIDDLE


def make_fragment_frame(channel: int, flags: int, body: bytes, total_size: int | None) -> bytes:
    """Like make_wire_frame, plus the u32 total size a FIRST frame carries."""
    header = struct.pack(">BBH", channel, flags, len(body))
    if (flags & FRAME_TYPE_MASK) == FRAME_FIRST:
        header += struct.pack(">I", total_size)
    return header + body
