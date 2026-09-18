import pytest

from aa_hmi.errors import IpcProtocolError
from aa_hmi.ipc import protocol as wire
from aa_hmi.touch_channel import TouchAction, TouchEvent


class FakeSocket:
    def __init__(self, data: bytes):
        self._buf = data

    def settimeout(self, t):
        pass

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


def test_make_and_read_ipc_message_roundtrip():
    msg = wire.make_ipc_message(wire.IpcMsgType.PING, b"")
    sock = FakeSocket(msg)
    msg_type, payload = wire.read_ipc_message(sock)
    assert msg_type == wire.IpcMsgType.PING
    assert payload == b""


def test_read_ipc_message_returns_none_on_clean_eof():
    assert wire.read_ipc_message(FakeSocket(b"")) is None


def test_read_ipc_message_raises_on_truncated_body():
    # length says 100 bytes but nothing follows
    import struct
    truncated = struct.pack(">I", 100)
    with pytest.raises(IpcProtocolError):
        wire.read_ipc_message(FakeSocket(truncated))


def test_hello_roundtrip():
    encoded = wire.encode_hello("my-ais-app", protocol_version=1)
    version, name = wire.decode_hello(encoded)
    assert version == 1
    assert name == "my-ais-app"


def test_hello_ack_roundtrip():
    encoded = wire.encode_hello_ack(frame_width=854, frame_height=480)
    ack = wire.decode_hello_ack(encoded)
    assert ack.frame_width == 854
    assert ack.frame_height == 480
    assert ack.pixel_format == wire.PixelFormat.RGB24
    assert ack.protocol_version == wire.PROTOCOL_VERSION


def test_frame_message_roundtrip_realistic_size():
    width, height = 854, 480
    pixel_data = bytes(width * height * 3)  # realistic full-size payload
    encoded = wire.encode_frame_msg(pixel_data, width, height)
    decoded = wire.decode_frame_msg(encoded)
    assert decoded.width == width
    assert decoded.height == height
    assert decoded.pixel_format == wire.PixelFormat.RGB24
    assert decoded.pixel_data == pixel_data


def test_frame_message_rejects_wrong_sized_payload():
    with pytest.raises(IpcProtocolError):
        wire.encode_frame_msg(b"too short", width=854, height=480)


def test_touch_message_raw_only_roundtrip():
    event = TouchEvent(raw=b"\x01\x02\x03")
    encoded = wire.encode_touch(event)
    decoded = wire.decode_touch(encoded)
    assert decoded.raw == b"\x01\x02\x03"
    assert decoded.is_structured is False
    assert decoded.x is None


def test_touch_message_structured_roundtrip():
    """The forward-compatibility mechanism: has_structured lets a future
    daemon send real x/y/action without breaking old clients that only
    read .raw."""
    event = TouchEvent(raw=b"\xAB\xCD", x=100, y=-50, pointer_id=1, action=TouchAction.DRAG)
    encoded = wire.encode_touch(event)
    decoded = wire.decode_touch(encoded)
    assert decoded.is_structured is True
    assert decoded.x == 100
    assert decoded.y == -50
    assert decoded.pointer_id == 1
    assert decoded.action == TouchAction.DRAG
    assert decoded.raw == b"\xAB\xCD"


def test_error_message_roundtrip():
    encoded = wire.encode_error(wire.IpcErrorCode.WRONG_GEOMETRY, "expected 854x480, got 100x100")
    code, message = wire.decode_error(encoded)
    assert code == wire.IpcErrorCode.WRONG_GEOMETRY
    assert "854x480" in message


def test_full_message_roundtrip_through_make_and_read():
    """End-to-end: pack a real message type through make_ipc_message,
    read it back through read_ipc_message, decode the payload."""
    event = TouchEvent(raw=b"\x18\x00")
    wire_bytes = wire.make_ipc_message(wire.IpcMsgType.TOUCH, wire.encode_touch(event))
    sock = FakeSocket(wire_bytes)
    msg_type, payload = wire.read_ipc_message(sock)
    assert msg_type == wire.IpcMsgType.TOUCH
    decoded = wire.decode_touch(payload)
    assert decoded.raw == b"\x18\x00"
