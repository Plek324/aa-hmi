"""ipc/client.py -- reference Python client library for talking to a
running `aa-hmi serve` daemon.

This is the ENTIRE surface a separate content-producing program (the
demo apps in examples/, and eventually a real one like an AIS plotter)
needs -- it never touches anything else in aa_hmi. If you're writing a
client in another language, docs/ipc-protocol.md has the full wire spec;
this module is just the Python reference implementation of it.

Usage:
    from aa_hmi.ipc.client import AaHmiClient

    with AaHmiClient() as client:
        client.send_frame(some_rgb24_bytes)          # must match client.frame_width/frame_height
        touch = client.poll_touch(timeout=0.0)        # non-blocking; None if nothing waiting
        if touch and touch.is_structured:
            print(touch.action.name, touch.x, touch.y)   # PRESS/DRAG/RELEASE, in frame coordinates
"""
from __future__ import annotations

import socket
from pathlib import Path

from ..errors import IpcProtocolError
from ..touch_channel import TouchEvent
from . import protocol as wire
from .server import default_socket_path


class AaHmiClient:
    def __init__(self, socket_path: Path | None = None, client_name: str = "aa-hmi-client"):
        self.socket_path = Path(socket_path) if socket_path else default_socket_path()
        self.client_name = client_name
        self._sock: socket.socket | None = None
        self.frame_width: int = wire.DEFAULT_FRAME_WIDTH
        self.frame_height: int = wire.DEFAULT_FRAME_HEIGHT
        self.pixel_format: wire.PixelFormat = wire.PixelFormat.RGB24

    def connect(self, timeout: float = 5.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(self.socket_path))
        self._sock.sendall(wire.make_ipc_message(wire.IpcMsgType.HELLO, wire.encode_hello(self.client_name)))
        result = wire.read_ipc_message(self._sock, timeout=timeout)
        if result is None:
            raise IpcProtocolError("daemon closed the connection before HELLO_ACK")
        msg_type, payload = result
        if msg_type == wire.IpcMsgType.ERROR:
            code, message = wire.decode_error(payload)
            raise IpcProtocolError(f"daemon refused connection ({code.name}): {message}")
        if msg_type != wire.IpcMsgType.HELLO_ACK:
            raise IpcProtocolError(f"expected HELLO_ACK, got msg_type={msg_type}")
        ack = wire.decode_hello_ack(payload)
        if ack.protocol_version != wire.PROTOCOL_VERSION:
            raise IpcProtocolError(
                f"protocol version mismatch: client speaks {wire.PROTOCOL_VERSION}, "
                f"daemon speaks {ack.protocol_version}"
            )
        self.frame_width, self.frame_height, self.pixel_format = ack.frame_width, ack.frame_height, ack.pixel_format

    def send_frame(self, pixel_data: bytes) -> None:
        """pixel_data must be exactly frame_width*frame_height*bytes_per_pixel
        bytes (RGB24: 3 bytes/pixel, row-major, no padding) -- matching
        client.frame_width/frame_height/pixel_format as reported by the
        daemon's HELLO_ACK. Raises IpcProtocolError immediately (client-side)
        on a size mismatch, rather than sending something the daemon will
        just reject anyway."""
        self._assert_connected()
        msg = wire.encode_frame_msg(pixel_data, self.frame_width, self.frame_height, self.pixel_format)
        self._sock.sendall(wire.make_ipc_message(wire.IpcMsgType.FRAME, msg))

    def poll_touch(self, timeout: float = 0.0) -> TouchEvent | None:
        """Returns the next available TouchEvent, or None if nothing
        arrives within `timeout` seconds (0.0 = don't block at all).
        A decoded event has .action (PRESS, then a stream of DRAG while
        the finger stays down, then RELEASE) and .x/.y in this client's
        frame coordinates -- a touch on the display's edge, outside the
        frame, can be negative or >= the frame size. .raw always holds
        the display's original message."""
        self._assert_connected()
        try:
            result = wire.read_ipc_message(self._sock, timeout=timeout if timeout > 0 else 0.001)
        except socket.timeout:
            return None
        except IpcProtocolError:
            return None
        if result is None:
            return None
        msg_type, payload = result
        if msg_type == wire.IpcMsgType.TOUCH:
            return wire.decode_touch(payload)
        if msg_type == wire.IpcMsgType.PONG:
            return None
        return None  # unexpected message type from the daemon -- ignore rather than raise

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _assert_connected(self) -> None:
        if self._sock is None:
            raise IpcProtocolError("not connected -- call connect() first (or use `with AaHmiClient() as client:`)")

    def __enter__(self) -> "AaHmiClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
