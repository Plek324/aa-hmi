"""ipc/server.py -- the daemon side of the local client <-> daemon socket.

Single active client for v1 (documented limitation, not solved now) -- a
second connection attempt while one is already attached gets ERROR +
close. Multi-client would need TOUCH fan-out to every connection and a
policy for concurrent FRAME sends (last-write-wins? reject?) -- noted as
a real follow-up in docs/ipc-protocol.md, not built.

Socket path resolution mirrors cache.default_cache_path()'s XDG-first
pattern (see default_socket_path below).
"""
from __future__ import annotations

import os
import socket
import stat
import threading
from pathlib import Path
from typing import Callable

from ..errors import IpcProtocolError
from ..log import log
from ..touch_channel import TouchEvent
from . import protocol as wire

FrameCallback = Callable[[wire.FrameMsg], None]


def default_socket_path() -> Path:
    """$XDG_RUNTIME_DIR/aa-hmi/video.sock, falling back to /tmp/aa-hmi/
    if unset -- same XDG-first-then-fallback shape as
    cache.default_cache_path(), just rooted at the runtime dir (this is
    ephemeral IPC state, not persistent config)."""
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(xdg_runtime) if xdg_runtime else Path("/tmp")
    return base / "aa-hmi" / "video.sock"


class IpcServer:
    def __init__(self, socket_path: Path | None = None,
                 frame_width: int = wire.DEFAULT_FRAME_WIDTH, frame_height: int = wire.DEFAULT_FRAME_HEIGHT):
        self.socket_path = Path(socket_path) if socket_path else default_socket_path()
        self.frame_width = frame_width
        self.frame_height = frame_height

        self._listen_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._client_sock: socket.socket | None = None
        self._client_lock = threading.Lock()  # guards _client_sock + writes to it
        self._client_thread: threading.Thread | None = None

        self._on_frame: FrameCallback | None = None

    def start(self, on_frame: FrameCallback) -> None:
        self._on_frame = on_frame
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.socket_path.exists():
            self.socket_path.unlink()  # stale socket from a previous crashed run
        self._listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listen_sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- local-machine-only by design
        self._listen_sock.listen(1)
        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True, name="ipc-accept")
        self._accept_thread.start()
        log(f"IPC server listening on {self.socket_path}")

    def _accept_loop(self) -> None:
        self._listen_sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, _ = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._client_lock:
                if self._client_sock is not None:
                    log("IPC: rejecting a second client -- one is already connected")
                    try:
                        conn.sendall(wire.make_ipc_message(
                            wire.IpcMsgType.ERROR,
                            wire.encode_error(wire.IpcErrorCode.CLIENT_ALREADY_CONNECTED,
                                               "aa-hmi only supports one connected client at a time (v1)"),
                        ))
                    except OSError:
                        pass
                    conn.close()
                    continue
                self._client_sock = conn
            self._client_thread = threading.Thread(target=self._handle_client, args=(conn,),
                                                     daemon=True, name="ipc-client")
            self._client_thread.start()

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            frame = wire.read_ipc_message(conn, timeout=5.0)
            if frame is None or frame[0] != wire.IpcMsgType.HELLO:
                self._send_error(conn, wire.IpcErrorCode.HELLO_REQUIRED, "expected HELLO as the first message")
                return
            version, client_name = wire.decode_hello(frame[1])
            log(f"IPC client connected: name={client_name!r} protocol_version={version}")
            conn.sendall(wire.make_ipc_message(
                wire.IpcMsgType.HELLO_ACK,
                wire.encode_hello_ack(self.frame_width, self.frame_height),
            ))

            while not self._stop.is_set():
                try:
                    result = wire.read_ipc_message(conn, timeout=1.0)
                except socket.timeout:
                    continue
                except IpcProtocolError as e:
                    log(f"IPC client protocol error, disconnecting: {e}")
                    return
                if result is None:
                    log("IPC client disconnected")
                    return
                msg_type, payload = result
                if msg_type == wire.IpcMsgType.FRAME:
                    self._handle_frame(conn, payload)
                elif msg_type == wire.IpcMsgType.PING:
                    conn.sendall(wire.make_ipc_message(wire.IpcMsgType.PONG, b""))
                # PONG/other unexpected types from the client are just ignored.
        except OSError as e:
            log(f"IPC client connection error: {e}")
        finally:
            with self._client_lock:
                if self._client_sock is conn:
                    self._client_sock = None
            try:
                conn.close()
            except OSError:
                pass

    def _handle_frame(self, conn: socket.socket, payload: bytes) -> None:
        try:
            frame_msg = wire.decode_frame_msg(payload)
        except IpcProtocolError as e:
            self._send_error(conn, wire.IpcErrorCode.INTERNAL, str(e))
            return
        if frame_msg.width != self.frame_width or frame_msg.height != self.frame_height:
            self._send_error(conn, wire.IpcErrorCode.WRONG_GEOMETRY,
                              f"expected {self.frame_width}x{self.frame_height}, "
                              f"got {frame_msg.width}x{frame_msg.height}")
            return
        if self._on_frame:
            try:
                self._on_frame(frame_msg)
            except Exception as e:  # noqa: BLE001 -- a bad frame must never kill the client connection
                log(f"on_frame callback raised: {type(e).__name__}: {e}")

    def _send_error(self, conn: socket.socket, code: wire.IpcErrorCode, message: str) -> None:
        try:
            conn.sendall(wire.make_ipc_message(wire.IpcMsgType.ERROR, wire.encode_error(code, message)))
        except OSError:
            pass

    def broadcast_touch(self, event: TouchEvent) -> None:
        """Best-effort: silently does nothing if no client is currently
        connected (there's no queue -- a client that wasn't listening
        just misses touch events that occurred while it was away, same
        as a real touchscreen driver with no buffering guarantee)."""
        with self._client_lock:
            conn = self._client_sock
            if conn is None:
                return
            try:
                conn.sendall(wire.make_ipc_message(wire.IpcMsgType.TOUCH, wire.encode_touch(event)))
            except OSError as e:
                log(f"failed to relay touch event to client: {e}")

    def stop(self) -> None:
        self._stop.set()
        with self._client_lock:
            if self._client_sock is not None:
                try:
                    self._client_sock.close()
                except OSError:
                    pass
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except OSError:
                pass
        if self._accept_thread is not None and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=2.0)
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        log("IPC server stopped")
