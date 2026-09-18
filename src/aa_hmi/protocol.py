"""protocol.py -- RFCOMM frame codec + a lenient protobuf-wire-format reader.

Two distinct things live here:

1. RFCOMM framing for the AA-Wireless Bluetooth bootstrap link:
   2-byte big-endian payload length, then 2-byte big-endian message-id,
   then the payload. This is NOT the same framing aa_pi2display's
   aa_session.py uses for the TCP/TLS video session (channel:u8, flags:u8,
   length:u16) -- RFCOMM is already a dedicated logical channel, so there's
   no channel/flags byte here.

2. A hand-rolled protobuf varint/string encoder and a *lenient* field
   decoder, adapted from aa_session.py's encode_varint_field /
   encode_string_field / read_varint / dump_protobuf. "Lenient" is
   deliberate and load-bearing, not a shortcut: a real captured
   WifiInfoResponse from a TF811BT display has security_mode=5, which is
   not a valid value in the public SecurityMode enum, and is missing the
   access_point_type field entirely despite the upstream .proto marking it
   `required`. A strict protobuf parser would reject that message outright.
   This module never validates enum values and never requires a field to
   be present -- callers ask for what they need and get None if it's
   missing, exactly like aa_session.py's own philosophy of trusting the
   real bytes over the spec.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

RFCOMM_HEADER_LEN = 4  # 2-byte length + 2-byte message-id


class FrameError(Exception):
    """Malformed RFCOMM frame (bad header, truncated payload, etc)."""


def make_rfcomm_frame(msg_id: int, payload: bytes) -> bytes:
    return struct.pack(">HH", len(payload), msg_id) + payload


def read_rfcomm_frame(sock: socket.socket, timeout: float | None = None) -> tuple[int, bytes] | None:
    """Read one complete RFCOMM frame: (msg_id, payload), or None on a
    clean EOF before any bytes of a new frame arrive.

    Raises FrameError on a partial header/payload followed by EOF (a real
    protocol violation, distinct from a clean "nothing more to read")."""
    if timeout is not None:
        sock.settimeout(timeout)
    header = _read_exact(sock, RFCOMM_HEADER_LEN, allow_empty=True)
    if header is None:
        return None
    length, msg_id = struct.unpack(">HH", header)
    payload = _read_exact(sock, length, allow_empty=False)
    if payload is None:
        raise FrameError(f"connection closed mid-payload (msg_id={msg_id:#06x}, wanted {length} bytes)")
    return msg_id, payload


def _read_exact(sock: socket.socket, n: int, *, allow_empty: bool) -> bytes | None:
    if n == 0:
        return b""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if allow_empty and not buf:
                return None
            raise FrameError(f"connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


# --- protobuf varint / string encoding (portable from aa_session.py) ---

def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def encode_varint_field(field_num: int, value: int) -> bytes:
    return bytes([(field_num << 3) | 0]) + encode_varint(value)


def encode_string_field(field_num: int, s: str) -> bytes:
    b = s.encode("utf-8")
    return bytes([(field_num << 3) | 2]) + encode_varint(len(b)) + b


def read_varint(data: bytes, i: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        if i >= len(data):
            raise FrameError("truncated varint")
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, i


# --- lenient field decoder ---

@dataclass
class Field:
    wire_type: int
    value: int | bytes  # varint value, or raw bytes for length-delimited


def decode_fields(data: bytes) -> dict[int, list[Field]]:
    """Walk tag/wire-type/value pairs and group them by field number.

    Stops (without raising) on an unrecognized wire type or a parse error
    partway through -- whatever fields were already decoded are still
    returned, matching aa_session.py's dump_protobuf's "stop, don't crash"
    behaviour. Real devices are the ground truth here, not the spec: an
    unknown trailing byte should never prevent reading the fields that DID
    parse cleanly.
    """
    fields: dict[int, list[Field]] = {}
    i = 0
    while i < len(data):
        try:
            tag, i = read_varint(data, i)
        except FrameError:
            break
        field_num, wire_type = tag >> 3, tag & 0x7
        try:
            if wire_type == 0:  # varint
                val, i = read_varint(data, i)
                fields.setdefault(field_num, []).append(Field(wire_type, val))
            elif wire_type == 1:  # 64-bit
                if i + 8 > len(data):
                    break
                fields.setdefault(field_num, []).append(Field(wire_type, data[i:i + 8]))
                i += 8
            elif wire_type == 2:  # length-delimited
                length, i = read_varint(data, i)
                if i + length > len(data):
                    break
                fields.setdefault(field_num, []).append(Field(wire_type, data[i:i + length]))
                i += length
            elif wire_type == 5:  # 32-bit
                if i + 4 > len(data):
                    break
                fields.setdefault(field_num, []).append(Field(wire_type, data[i:i + 4]))
                i += 4
            else:
                break  # unknown wire type -- stop, keep what we have
        except FrameError:
            break
    return fields


def get_string(fields: dict[int, list[Field]], field_num: int) -> str | None:
    vals = fields.get(field_num)
    if not vals or not isinstance(vals[0].value, (bytes, bytearray)):
        return None
    try:
        return vals[0].value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_varint(fields: dict[int, list[Field]], field_num: int) -> int | None:
    vals = fields.get(field_num)
    if not vals or not isinstance(vals[0].value, int):
        return None
    return vals[0].value


def dump_fields(data: bytes, indent: int = 1) -> list[str]:
    """Human-readable dump for debugging -- same purpose as
    aa_session.py's dump_protobuf, built on top of decode_fields."""
    lines = []
    pad = "  " * indent
    for field_num, vals in decode_fields(data).items():
        for f in vals:
            if f.wire_type == 0:
                lines.append(f"{pad}field {field_num} (varint) = {f.value}")
            elif f.wire_type == 2:
                assert isinstance(f.value, (bytes, bytearray))
                lines.append(f"{pad}field {field_num} (bytes) len={len(f.value)} = {f.value.hex()}")
            else:
                lines.append(f"{pad}field {field_num} (wire_type={f.wire_type}) = {f.value!r}")
    return lines
