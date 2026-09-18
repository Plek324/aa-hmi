"""video_session.py -- the TCP/TLS Android-Auto-Wireless video session.

Restructured from aa_pi2display's aa_session.py (a script with local
closures) into a reusable class with explicit state, so a daemon can hold
one open indefinitely, react to failure at any stage, and rebuild it on
reconnect. The handshake sequence itself is proven working end to end
(including a real TF811BT/Podofo display) -- ported as-is, not redesigned.
See docs/video-protocol-notes.md for the full protocol writeup.

Threading model: one background reader thread (started once the TLS
handshake completes) continuously drains incoming wire frames -- this is
required to keep the TLS stream from stalling on unread data (ACKs etc),
and it's how touch events (channel 1) arrive asynchronously. A single lock
(`_tls_lock`) guards every touch of the TLS object and its MemoryBIOs,
since neither is documented thread-safe for concurrent use; the same lock
also serializes the actual `sock.sendall()` calls so two threads writing
at once can never interleave bytes on the wire.
"""
from __future__ import annotations

import socket
import ssl
import struct
import threading
import time
from enum import Enum, auto
from pathlib import Path
from typing import Callable

from . import video_protocol as wire
from . import video_messages as m
from .errors import TlsHandshakeError, VideoSessionError
from .log import log
from .protocol import encode_string_field, encode_varint_field

TouchCallback = Callable[[bytes], None]


class VideoSessionState(Enum):
    CONNECTING = auto()
    HANDSHAKING = auto()
    OPEN = auto()
    CLOSED = auto()


class VideoSession:
    def __init__(self, display_ip: str, cert_file: Path, key_file: Path,
                 display_port: int = m.DISPLAY_PORT, keylog_path: str | None = None):
        self.display_ip = display_ip
        self.display_port = display_port
        self.cert_file = Path(cert_file)
        self.key_file = Path(key_file)
        self.keylog_path = keylog_path

        self.state = VideoSessionState.CONNECTING
        self._sock: socket.socket | None = None
        self._tls_obj: ssl.SSLObject | None = None
        self._incoming: ssl.MemoryBIO | None = None
        self._outgoing: ssl.MemoryBIO | None = None
        self._tls_lock = threading.Lock()

        self._reader_thread: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._on_touch_raw: TouchCallback | None = None

    # --- connect + handshake (synchronous, no reader thread yet) ---

    def connect_and_handshake(self, timeout: float = 10.0) -> None:
        self.state = VideoSessionState.CONNECTING
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        log(f"connecting to {self.display_ip}:{self.display_port} ...")
        self._sock.connect((self.display_ip, self.display_port))

        frame = wire.read_wire_frame(self._sock)
        if frame is None:
            raise VideoSessionError(
                "connection closed before VersionRequest -- is the display's TCP port actually "
                "open? (needs a fresh Bluetooth trigger first, see orchestrate.py)"
            )
        channel, _flags, payload = frame
        msg_id = struct.unpack(">H", payload[:2])[0]
        log(f"<- VersionRequest? channel={channel} msg_id={msg_id}")
        self._sock.sendall(wire.make_wire_frame(
            m.CHANNEL_CONTROL, 0x03,
            struct.pack(">H", m.MSG_VERSION_RESPONSE) + struct.pack(">HHH", 1, 7, 0),
        ))
        log("-> sent VersionResponse")

        self.state = VideoSessionState.HANDSHAKING
        self._do_tls_handshake(timeout)

        # AuthComplete -- plaintext, last unencrypted message, content ignored.
        frame = wire.read_wire_frame(self._sock)
        if frame is None:
            raise VideoSessionError("connection closed waiting for AuthComplete")
        log("<- AuthComplete")

        self._stop_reader.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="video-session-reader")
        self._reader_thread.start()

        self.state = VideoSessionState.OPEN

    def _do_tls_handshake(self, timeout: float) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(self.cert_file), keyfile=str(self.key_file))
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if self.keylog_path:
            ctx.keylog_filename = self.keylog_path
            log(f"TLS keylog enabled: {self.keylog_path}")
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._tls_obj = ctx.wrap_bio(self._incoming, self._outgoing, server_side=True)

        self._sock.settimeout(timeout)
        handshake_done = False
        for _ in range(200):
            try:
                self._tls_obj.do_handshake()
                handshake_done = True
                log("*** TLS HANDSHAKE COMPLETE ***")
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as e:
                raise TlsHandshakeError(f"TLS error: {e}") from e
            # CRITICAL, proven-the-hard-way ordering: flush outgoing.read()
            # AFTER a successful do_handshake(), and only break after that
            # flush -- a naive do_handshake(); break never sends our own
            # final Finished message. The display (TLS client role) then
            # just sits waiting forever and never sends AuthComplete --
            # looks exactly like "handshake succeeded but display went
            # silent," not an obvious bug. See docs/video-protocol-notes.md.
            out_bytes = self._outgoing.read()
            if out_bytes:
                self._sock.sendall(wire.make_wire_frame(
                    m.CHANNEL_CONTROL, 0x03, struct.pack(">H", m.MSG_SSL_HANDSHAKE) + out_bytes,
                ))
            if handshake_done:
                return
            frame = wire.read_wire_frame(self._sock)
            if frame is None:
                raise TlsHandshakeError("connection closed during TLS handshake")
            channel, _flags, payload = frame
            msg_id = struct.unpack(">H", payload[:2])[0]
            self._incoming.write(payload[2:] if channel == m.CHANNEL_CONTROL and msg_id == m.MSG_SSL_HANDSHAKE
                                  else payload)
        raise TlsHandshakeError("TLS handshake did not complete within 200 iterations")

    # --- sending (thread-safe, usable from any thread once OPEN) ---

    def _send_encrypted_locked(self, channel: int, flags: int, msg_id: int, body: bytes) -> None:
        with self._tls_lock:
            self._tls_obj.write(struct.pack(">H", msg_id) + body)
            ciphertext = self._outgoing.read()
            for record in wire.split_tls_records(ciphertext):
                self._sock.sendall(wire.make_wire_frame(channel, flags, record))

    def open_video_channel(self) -> None:
        self._assert_open()
        sdr = encode_string_field(4, "aa-hmi") + encode_string_field(5, "aa-hmi project")
        self._send_encrypted_locked(m.CHANNEL_CONTROL, 0x0B, m.MSG_SERVICE_DISCOVERY_REQUEST, sdr)

        avsr = encode_varint_field(1, 0)  # config_index = 0
        self._send_encrypted_locked(m.CHANNEL_VIDEO, m.FLAGS_CHANNEL_OPEN, m.AV_SETUP_REQUEST, avsr)

        vfr = encode_varint_field(1, 0) + encode_varint_field(2, 1) + encode_varint_field(3, 1)
        self._send_encrypted_locked(m.CHANNEL_VIDEO, m.FLAGS_NORMAL, m.VIDEO_FOCUS_REQUEST, vfr)
        log("video channel opened, focus requested")

    def open_touch_channel(self, on_touch_raw: TouchCallback) -> None:
        """Opens channel 1 and registers a callback invoked (from the
        background reader thread -- keep it fast/non-blocking) with the
        raw decrypted payload of every INPUT_EVENT_INDICATION frame. See
        touch_channel.py for turning that into a (still placeholder)
        TouchEvent."""
        self._assert_open()
        self._on_touch_raw = on_touch_raw
        # Best-guess empty open frame -- INPUT has no setup-request-
        # equivalent message in the proto, unlike the AV channels, so
        # there's nothing more specific to send. Confirmed working
        # (content doesn't seem to matter, same as channel 3's open).
        self._send_encrypted_locked(m.CHANNEL_INPUT, m.FLAGS_CHANNEL_OPEN, 0x0000, b"")
        log("touch (input) channel opened")

    def send_frame(self, nal_bytes: bytes, timestamp: int) -> None:
        """timestamp is a small, stream-relative counter (microseconds
        since this session's own start), NOT wall-clock time -- the
        display has no RTC and can't make sense of absolute epoch time."""
        self._assert_open()
        body = struct.pack(">Q", timestamp) + nal_bytes
        self._send_encrypted_locked(m.CHANNEL_VIDEO, m.FLAGS_NORMAL, m.AV_MEDIA_WITH_TIMESTAMP_INDICATION, body)

    def _assert_open(self) -> None:
        if self.state != VideoSessionState.OPEN:
            raise VideoSessionError(f"video session is not open (state={self.state})")

    def is_alive(self) -> bool:
        """True if the session believes it's open AND its background
        reader thread is still actually running -- a daemon's reconnect
        loop should poll this rather than just trusting `state`, since
        the reader thread can exit (connection dropped) without anything
        else having noticed yet."""
        return (self.state == VideoSessionState.OPEN
                and self._reader_thread is not None
                and self._reader_thread.is_alive())

    # --- background reader ---

    def _reader_loop(self) -> None:
        self._sock.settimeout(1.0)  # short timeout so the stop event is checked promptly
        while not self._stop_reader.is_set():
            try:
                frame = wire.read_wire_frame(self._sock)
            except socket.timeout:
                continue
            except (OSError, wire.WireFrameError) as e:
                if not self._stop_reader.is_set():
                    log(f"video session reader stopped: {type(e).__name__}: {e}")
                return
            if frame is None:
                log("video session: connection closed by display")
                return
            channel, _flags, payload = frame
            try:
                with self._tls_lock:
                    self._incoming.write(payload)
                    plaintext = self._tls_obj.read(65536)
            except ssl.SSLWantReadError:
                continue
            except ssl.SSLError as e:
                log(f"video session reader: TLS error decoding incoming frame: {e}")
                continue
            if not plaintext or len(plaintext) < 2:
                continue
            msg_id = struct.unpack(">H", plaintext[:2])[0]
            body = plaintext[2:]
            if channel == m.CHANNEL_INPUT and msg_id == m.INPUT_EVENT_INDICATION and self._on_touch_raw:
                try:
                    self._on_touch_raw(body)
                except Exception as e:  # noqa: BLE001 -- a bad callback must never kill the reader
                    log(f"touch callback raised: {type(e).__name__}: {e}")
            # Everything else (ACKs, status indications, etc) is
            # deliberately just drained here and not acted on -- reading
            # it is what matters, to keep the TLS/TCP stream from
            # stalling on unread incoming data.

    # --- shutdown ---

    def close(self) -> None:
        """Always safe to call, including on a session that never fully
        opened, or twice. Runs the proven clean-shutdown sequence
        (unfocus -> AVChannelStopIndication -> ShutdownRequest) whenever
        the session was actually open enough for that to make sense."""
        if self.state == VideoSessionState.CLOSED:
            return
        was_open = self.state == VideoSessionState.OPEN
        self.state = VideoSessionState.CLOSED
        self._stop_reader.set()
        if was_open:
            try:
                log("requesting video focus UNFOCUSED, then AVChannelStopIndication")
                vfr_unfocus = encode_varint_field(1, 0) + encode_varint_field(2, 2) + encode_varint_field(3, 1)
                self._send_encrypted_locked(m.CHANNEL_VIDEO, m.FLAGS_NORMAL, m.VIDEO_FOCUS_REQUEST, vfr_unfocus)
                self._send_encrypted_locked(m.CHANNEL_VIDEO, m.FLAGS_NORMAL, m.AV_STOP_INDICATION, b"")
                log("sending clean ShutdownRequest")
                self._send_encrypted_locked(m.CHANNEL_CONTROL, m.FLAGS_NORMAL, m.MSG_SHUTDOWN_REQUEST,
                                             encode_varint_field(1, 1))
                time.sleep(0.3)  # give the display a moment to act on shutdown before we close the socket
            except Exception as e:  # noqa: BLE001 -- shutdown must never raise past this point
                log(f"clean shutdown sequence failed (continuing to close anyway): {type(e).__name__}: {e}")
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        log("video session closed")
