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


def split_tls_records(data: bytes) -> list[bytes]:
    """Split concatenated TLS records (as drained from an outgoing
    MemoryBIO after a single write) into a list of individual complete
    record byte-strings (5-byte header + payload each).

    A single TLS 1.2 record can carry at most 16384 bytes of plaintext
    (RFC 5246), so any write() larger than that gets silently split into
    multiple records by the TLS layer -- and the display's decoder does
    not handle multiple TLS records bundled under one wire frame
    (confirmed: a >16KB message sent as one wire frame wrapping two TLS
    records produced a black screen). Send each returned chunk as its own
    separate wire frame instead. Proven fix, ported verbatim from
    aa_pi2display's aa_session.py.
    """
    records = []
    i = 0
    while i + 5 <= len(data):
        rlen = struct.unpack(">H", data[i + 3:i + 5])[0]
        total = 5 + rlen
        records.append(data[i:i + total])
        i += total
    return records
