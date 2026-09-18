"""ipc/protocol.py -- wire codec for the local client <-> daemon socket.

A THIRD distinct framing format in this codebase (see protocol.py for
RFCOMM's u16-length framing, video_protocol.py for the TCP video
session's channel/flags/u16-length framing) -- needed because a single
854x480 RGB24 frame is 854*480*3 = 1,229,760 bytes, already well over a
u16 length field's 65535-byte ceiling. Full spec: docs/ipc-protocol.md.

Framing: length:u32 BE (covers msg_type + payload) + msg_type:u8 + payload.

This module only encodes/decodes message bytes -- it doesn't touch a
socket directly except in read_ipc_message (the one place that needs to,
mirroring protocol.py's read_rfcomm_frame / video_protocol.py's
read_wire_frame).
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from enum import IntEnum

from ..errors import IpcProtocolError
from ..touch_channel import TouchAction, TouchEvent

PROTOCOL_VERSION = 1

DEFAULT_FRAME_WIDTH = 854
DEFAULT_FRAME_HEIGHT = 480

HEADER_LEN = 4  # length:u32, covers msg_type + payload that follows it


class IpcMsgType(IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    FRAME = 3
    TOUCH = 4
    PING = 5
    PONG = 6
    ERROR = 7


class PixelFormat(IntEnum):
    RGB24 = 1

    @property
    def bytes_per_pixel(self) -> int:
        return {PixelFormat.RGB24: 3}[self]


class IpcErrorCode(IntEnum):
    UNKNOWN = 0
    PROTOCOL_VERSION_MISMATCH = 1
    HELLO_REQUIRED = 2
    WRONG_GEOMETRY = 3
    CLIENT_ALREADY_CONNECTED = 4
    INTERNAL = 5


# --- low-level framing ---

def make_ipc_message(msg_type: IpcMsgType, payload: bytes) -> bytes:
    body = struct.pack(">B", msg_type) + payload
    return struct.pack(">I", len(body)) + body


def read_ipc_message(sock: socket.socket, timeout: float | None = None) -> tuple[IpcMsgType, bytes] | None:
    """Returns (msg_type, payload), or None on a clean EOF before any
    bytes of a new message arrive."""
    if timeout is not None:
        sock.settimeout(timeout)
    length_bytes = _read_exact(sock, HEADER_LEN, allow_empty=True)
    if length_bytes is None:
        return None
    (length,) = struct.unpack(">I", length_bytes)
    if length < 1:
        raise IpcProtocolError(f"malformed message: length={length} too short to hold a msg_type byte")
    body = _read_exact(sock, length, allow_empty=False)
    if body is None:
        raise IpcProtocolError(f"connection closed mid-message (wanted {length} bytes)")
    try:
        msg_type = IpcMsgType(body[0])
    except ValueError as e:
        raise IpcProtocolError(f"unknown msg_type byte {body[0]:#04x}") from e
    return msg_type, body[1:]


def _read_exact(sock: socket.socket, n: int, *, allow_empty: bool) -> bytes | None:
    if n == 0:
        return b""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if allow_empty and not buf:
                return None
            raise IpcProtocolError(f"connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


# --- plain (non-protobuf) length-prefixed strings, u16 length ---

def _encode_str(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > 0xFFFF:
        raise IpcProtocolError(f"string too long to encode ({len(b)} bytes, max 65535)")
    return struct.pack(">H", len(b)) + b


def _decode_str(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise IpcProtocolError("truncated string length prefix")
    (length,) = struct.unpack(">H", data[offset:offset + 2])
    offset += 2
    if offset + length > len(data):
        raise IpcProtocolError("truncated string data")
    return data[offset:offset + length].decode("utf-8", errors="replace"), offset + length


# --- HELLO / HELLO_ACK ---

def encode_hello(client_name: str, protocol_version: int = PROTOCOL_VERSION) -> bytes:
    return struct.pack(">H", protocol_version) + _encode_str(client_name)


def decode_hello(payload: bytes) -> tuple[int, str]:
    if len(payload) < 2:
        raise IpcProtocolError("HELLO payload too short")
    (version,) = struct.unpack(">H", payload[:2])
    name, _ = _decode_str(payload, 2)
    return version, name


def encode_hello_ack(frame_width: int = DEFAULT_FRAME_WIDTH, frame_height: int = DEFAULT_FRAME_HEIGHT,
                      pixel_format: PixelFormat = PixelFormat.RGB24,
                      protocol_version: int = PROTOCOL_VERSION) -> bytes:
    return struct.pack(">HHHB", protocol_version, frame_width, frame_height, int(pixel_format))


@dataclass
class HelloAck:
    protocol_version: int
    frame_width: int
    frame_height: int
    pixel_format: PixelFormat


def decode_hello_ack(payload: bytes) -> HelloAck:
    if len(payload) != 7:
        raise IpcProtocolError(f"HELLO_ACK payload must be 7 bytes, got {len(payload)}")
    version, width, height, fmt = struct.unpack(">HHHB", payload)
    return HelloAck(version, width, height, PixelFormat(fmt))


# --- FRAME ---

@dataclass
class FrameMsg:
    width: int
    height: int
    pixel_format: PixelFormat
    pixel_data: bytes


def encode_frame_msg(pixel_data: bytes, width: int = DEFAULT_FRAME_WIDTH, height: int = DEFAULT_FRAME_HEIGHT,
                      pixel_format: PixelFormat = PixelFormat.RGB24) -> bytes:
    expected = width * height * pixel_format.bytes_per_pixel
    if len(pixel_data) != expected:
        raise IpcProtocolError(f"expected {expected} bytes of pixel data for {width}x{height} "
                                f"{pixel_format.name}, got {len(pixel_data)}")
    return struct.pack(">HHBB", width, height, int(pixel_format), 0) + pixel_data


def decode_frame_msg(payload: bytes) -> FrameMsg:
    if len(payload) < 6:
        raise IpcProtocolError("FRAME payload too short")
    width, height, fmt, _reserved = struct.unpack(">HHBB", payload[:6])
    pixel_format = PixelFormat(fmt)
    pixel_data = payload[6:]
    expected = width * height * pixel_format.bytes_per_pixel
    if len(pixel_data) != expected:
        raise IpcProtocolError(f"FRAME declares {width}x{height} {pixel_format.name} "
                                f"({expected} bytes) but carries {len(pixel_data)}")
    return FrameMsg(width, height, pixel_format, pixel_data)


# --- TOUCH ---

def encode_touch(event: TouchEvent) -> bytes:
    out = struct.pack(">B", event.schema) + struct.pack(">H", len(event.raw)) + event.raw
    if event.is_structured:
        out += struct.pack(">B", 1) + struct.pack(">hhBB", event.x, event.y, event.pointer_id, int(event.action))
    else:
        out += struct.pack(">B", 0)
    return out


def decode_touch(payload: bytes) -> TouchEvent:
    if len(payload) < 3:
        raise IpcProtocolError("TOUCH payload too short")
    schema = payload[0]
    (raw_len,) = struct.unpack(">H", payload[1:3])
    offset = 3
    if offset + raw_len > len(payload):
        raise IpcProtocolError("TOUCH payload: truncated raw bytes")
    raw = payload[offset:offset + raw_len]
    offset += raw_len
    if offset >= len(payload):
        raise IpcProtocolError("TOUCH payload missing has_structured byte")
    has_structured = payload[offset]
    offset += 1
    if not has_structured:
        return TouchEvent(raw=raw, schema=schema)
    if offset + 6 > len(payload):
        raise IpcProtocolError("TOUCH payload: truncated structured fields")
    x, y, pointer_id, action = struct.unpack(">hhBB", payload[offset:offset + 6])
    return TouchEvent(raw=raw, schema=schema, x=x, y=y, pointer_id=pointer_id, action=TouchAction(action))


# --- ERROR ---

def encode_error(code: IpcErrorCode, message: str) -> bytes:
    return struct.pack(">B", int(code)) + _encode_str(message)


def decode_error(payload: bytes) -> tuple[IpcErrorCode, str]:
    if len(payload) < 1:
        raise IpcProtocolError("ERROR payload too short")
    code = IpcErrorCode(payload[0])
    message, _ = _decode_str(payload, 1)
    return code, message
