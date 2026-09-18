"""IpcServer tests against a real Unix domain socket in a temp dir.

Gated on AF_UNIX availability -- not present on this project's Windows dev
machine's Python build, but always available on the real Linux target
(Raspberry Pi / any modern Linux), which is where this actually needs to
work. Mirrors the project's existing pattern of skipping platform-specific
tests (see test_cache.py's 0600-permissions test, skipped on win32).
"""
import socket
import time

import pytest

from aa_hmi.ipc import protocol as wire
from aa_hmi.ipc.server import IpcServer

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX not available on this platform")


def _connect_raw(socket_path, timeout=2.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(socket_path))
    return sock


@pytest.fixture
def server(tmp_path):
    s = IpcServer(socket_path=tmp_path / "test.sock")
    s.start(on_frame=lambda frame_msg: None)
    time.sleep(0.05)  # let the accept thread actually start listening
    yield s
    s.stop()


def test_hello_required_before_anything_else(server):
    sock = _connect_raw(server.socket_path)
    # Send a FRAME (well-formed) before HELLO -- server must reject it.
    bogus_frame = wire.make_ipc_message(wire.IpcMsgType.FRAME, b"\x00" * 10)
    sock.sendall(bogus_frame)
    msg_type, payload = wire.read_ipc_message(sock, timeout=2.0)
    assert msg_type == wire.IpcMsgType.ERROR
    code, _ = wire.decode_error(payload)
    assert code == wire.IpcErrorCode.HELLO_REQUIRED


def test_hello_gets_hello_ack_with_expected_geometry(server):
    sock = _connect_raw(server.socket_path)
    sock.sendall(wire.make_ipc_message(wire.IpcMsgType.HELLO, wire.encode_hello("test-client")))
    msg_type, payload = wire.read_ipc_message(sock, timeout=2.0)
    assert msg_type == wire.IpcMsgType.HELLO_ACK
    ack = wire.decode_hello_ack(payload)
    assert ack.frame_width == wire.DEFAULT_FRAME_WIDTH
    assert ack.frame_height == wire.DEFAULT_FRAME_HEIGHT


def test_wrong_geometry_frame_gets_error(server):
    sock = _connect_raw(server.socket_path)
    sock.sendall(wire.make_ipc_message(wire.IpcMsgType.HELLO, wire.encode_hello("test-client")))
    wire.read_ipc_message(sock, timeout=2.0)  # drain HELLO_ACK

    wrong_size_frame = wire.encode_frame_msg(bytes(10 * 10 * 3), width=10, height=10)
    sock.sendall(wire.make_ipc_message(wire.IpcMsgType.FRAME, wrong_size_frame))
    msg_type, payload = wire.read_ipc_message(sock, timeout=2.0)
    assert msg_type == wire.IpcMsgType.ERROR
    code, _ = wire.decode_error(payload)
    assert code == wire.IpcErrorCode.WRONG_GEOMETRY


def test_on_frame_callback_invoked_for_correctly_sized_frame(tmp_path):
    received = []
    s = IpcServer(socket_path=tmp_path / "test2.sock")
    s.start(on_frame=lambda frame_msg: received.append(frame_msg))
    time.sleep(0.05)
    try:
        sock = _connect_raw(s.socket_path)
        sock.sendall(wire.make_ipc_message(wire.IpcMsgType.HELLO, wire.encode_hello("test-client")))
        wire.read_ipc_message(sock, timeout=2.0)

        pixel_data = bytes(wire.DEFAULT_FRAME_WIDTH * wire.DEFAULT_FRAME_HEIGHT * 3)
        good_frame = wire.encode_frame_msg(pixel_data)
        sock.sendall(wire.make_ipc_message(wire.IpcMsgType.FRAME, good_frame))

        deadline = time.monotonic() + 2.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(received) == 1
        assert received[0].width == wire.DEFAULT_FRAME_WIDTH
    finally:
        s.stop()


def test_second_concurrent_client_is_rejected(server):
    first = _connect_raw(server.socket_path)
    first.sendall(wire.make_ipc_message(wire.IpcMsgType.HELLO, wire.encode_hello("first")))
    wire.read_ipc_message(first, timeout=2.0)  # drain HELLO_ACK -- first client is now "attached"

    second = _connect_raw(server.socket_path)
    second.sendall(wire.make_ipc_message(wire.IpcMsgType.HELLO, wire.encode_hello("second")))
    msg_type, payload = wire.read_ipc_message(second, timeout=2.0)
    assert msg_type == wire.IpcMsgType.ERROR
    code, _ = wire.decode_error(payload)
    assert code == wire.IpcErrorCode.CLIENT_ALREADY_CONNECTED


def test_broadcast_touch_does_nothing_with_no_client(tmp_path):
    from aa_hmi.touch_channel import TouchEvent
    s = IpcServer(socket_path=tmp_path / "test3.sock")
    s.start(on_frame=lambda frame_msg: None)
    try:
        s.broadcast_touch(TouchEvent(raw=b"\x00"))  # must not raise
    finally:
        s.stop()


def test_broadcast_touch_reaches_connected_client(tmp_path):
    from aa_hmi.touch_channel import TouchEvent
    s = IpcServer(socket_path=tmp_path / "test4.sock")
    s.start(on_frame=lambda frame_msg: None)
    time.sleep(0.05)
    try:
        sock = _connect_raw(s.socket_path)
        sock.sendall(wire.make_ipc_message(wire.IpcMsgType.HELLO, wire.encode_hello("test-client")))
        wire.read_ipc_message(sock, timeout=2.0)  # drain HELLO_ACK

        time.sleep(0.05)  # let the server register this connection as "the" client
        s.broadcast_touch(TouchEvent(raw=b"\x18\x00"))

        msg_type, payload = wire.read_ipc_message(sock, timeout=2.0)
        assert msg_type == wire.IpcMsgType.TOUCH
        event = wire.decode_touch(payload)
        assert event.raw == b"\x18\x00"
    finally:
        s.stop()
