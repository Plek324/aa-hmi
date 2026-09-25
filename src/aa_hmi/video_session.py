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
from .protocol import decode_fields, dump_fields, encode_string_field, encode_varint_field, get_varint

TouchCallback = Callable[[bytes], None]


class VideoSessionState(Enum):
    CONNECTING = auto()
    HANDSHAKING = auto()
    OPEN = auto()
    CLOSED = auto()


class VideoSession:
    def __init__(self, display_ip: str, cert_file: Path, key_file: Path,
                 display_port: int = m.DISPLAY_PORT, keylog_path: str | None = None,
                 verbose: bool = False):
        self.display_ip = display_ip
        self.display_port = display_port
        self.cert_file = Path(cert_file)
        self.key_file = Path(key_file)
        self.keylog_path = keylog_path
        self.verbose = verbose

        # Media flow accounting -- see _handle_incoming and ack_stats().
        # Added to diagnose a real freeze (display stops updating while
        # every send still succeeds): lets us see whether the display's
        # acks stop, slow down, or change when that happens.
        self.frames_sent = 0
        self.fragmented_messages = 0  # messages that needed FIRST/MIDDLE/LAST frames
        self.acks_received = 0      # sum of ack "value" fields (frames acked)
        self.ack_messages = 0       # number of ack messages
        self.max_unacked: int | None = None  # from AV_SETUP_RESPONSE, if the display sends one
        self._last_send = 0.0
        self._last_ack = 0.0
        self._last_ack_session: int | None = None
        self._last_ack_value: int | None = None

        self.state = VideoSessionState.CONNECTING
        self._sock: socket.socket | None = None
        self._tls_obj: ssl.SSLObject | None = None
        self._incoming: ssl.MemoryBIO | None = None
        self._outgoing: ssl.MemoryBIO | None = None
        self._tls_lock = threading.Lock()

        self._reader_thread: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._on_touch_raw: TouchCallback | None = None
        self._last_activity: float = 0.0  # time.monotonic() of the last frame RECEIVED from the display

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
        self._last_activity = time.monotonic()  # start the liveness clock from here, not object construction
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
        plaintext = struct.pack(">H", msg_id) + body
        chunks = wire.split_plaintext(plaintext)
        if len(chunks) > 1:
            self.fragmented_messages += 1
        with self._tls_lock:
            for index, chunk in enumerate(chunks):
                # Each chunk is <= 16384 bytes, so it encrypts to exactly
                # one TLS record -- one record per wire frame.
                self._tls_obj.write(chunk)
                ciphertext = self._outgoing.read()
                self._sock.sendall(wire.make_fragment_frame(
                    channel, wire.fragment_flags(flags, index, len(chunks)), ciphertext, len(plaintext)))

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
        self.frames_sent += 1
        self._last_send = time.monotonic()

    def ack_stats(self) -> dict:
        """Snapshot of media flow: how many media messages we've sent, how
        many the display has acked, and how long since each. `outstanding`
        growing without bound (or `seconds_since_ack` climbing while
        `seconds_since_send` stays small) means the display has stopped
        acking what we send."""
        now = time.monotonic()
        return {
            "frames_sent": self.frames_sent,
            "fragmented_messages": self.fragmented_messages,
            "acks_received": self.acks_received,
            "ack_messages": self.ack_messages,
            "outstanding": self.frames_sent - self.acks_received,
            "max_unacked": self.max_unacked,
            "seconds_since_send": (now - self._last_send) if self._last_send else None,
            "seconds_since_ack": (now - self._last_ack) if self._last_ack else None,
            "last_ack_session": self._last_ack_session,
            "last_ack_value": self._last_ack_value,
        }

    def _assert_open(self) -> None:
        if self.state != VideoSessionState.OPEN:
            raise VideoSessionError(f"video session is not open (state={self.state})")

    def is_alive(self, *, liveness_timeout: float | None = None) -> bool:
        """True if the session believes it's open AND its background
        reader thread is still actually running -- a daemon's reconnect
        loop should poll this rather than just trusting `state`, since
        the reader thread can exit (connection dropped) without anything
        else having noticed yet.

        liveness_timeout, if given, ALSO requires that the display has
        sent us something -- anything, an ACK, a status indication, not
        necessarily a touch event -- within that many seconds. Found live
        (2026-09-22) to matter: the display's own video decoder can
        apparently wedge silently while the underlying TCP/TLS connection
        stays completely healthy from our side -- every send_frame() call
        keeps succeeding (the OS happily accepts the bytes into its send
        buffer; nothing on our end ever sees an error), but the display
        stops updating and, evidently, stops sending anything back too.
        Without this check, `is_alive()` (and thus the daemon's reconnect
        watchdog) had no way to ever notice -- "aa-hmi keeps printing
        sent frame messages, totally unaware something is off," reported
        verbatim. That freeze turned out to be caused by sending one image
        as several messages (fixed, see docs/video-protocol-notes.md) --
        and the display kept acking throughout it, so this check would not
        have caught that one. Kept as a safety net for a display that goes
        completely silent. Omitted by default (None) so existing
        callers that just want "is the transport still up" are unaffected."""
        if self.state != VideoSessionState.OPEN:
            return False
        if self._reader_thread is None or not self._reader_thread.is_alive():
            return False
        if liveness_timeout is not None and (time.monotonic() - self._last_activity) > liveness_timeout:
            return False
        return True

    def seconds_since_last_activity(self) -> float:
        """How long since the display last sent us anything at all --
        exposed mainly so callers can log a clear reason when
        is_alive(liveness_timeout=...) trips."""
        return time.monotonic() - self._last_activity

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
            self._last_activity = time.monotonic()
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
            self._handle_incoming(channel, msg_id, body)

    def _handle_incoming(self, channel: int, msg_id: int, body: bytes) -> None:
        """Dispatch one decrypted message from the display. Previously
        everything except touch was silently drained; now acks are
        counted and anything unexpected is logged, so a freeze can be
        correlated with what the display was (or stopped) saying."""
        if channel == m.CHANNEL_INPUT and msg_id == m.INPUT_EVENT_INDICATION:
            if self._on_touch_raw:
                try:
                    self._on_touch_raw(body)
                except Exception as e:  # noqa: BLE001 -- a bad callback must never kill the reader
                    log(f"touch callback raised: {type(e).__name__}: {e}")
            return

        if channel == m.CHANNEL_VIDEO and msg_id == m.AV_MEDIA_ACK_INDICATION:
            fields = decode_fields(body)
            session = get_varint(fields, 1)
            value = get_varint(fields, 2)
            self.ack_messages += 1
            self.acks_received += value if value is not None else 1
            self._last_ack = time.monotonic()
            # Log only when the ack's shape changes (every ack so far has
            # been session=0 value=1) -- a change is exactly the kind of
            # thing that might coincide with a freeze.
            if (session, value) != (self._last_ack_session, self._last_ack_value):
                log(f"<- media ack: session={session} value={value} (ack #{self.ack_messages}, "
                    f"frames sent so far={self.frames_sent})")
            self._last_ack_session, self._last_ack_value = session, value
            return

        if channel == m.CHANNEL_VIDEO and msg_id == m.AV_SETUP_RESPONSE:
            fields = decode_fields(body)
            self.max_unacked = get_varint(fields, 2)
            log(f"<- AV setup response: status={get_varint(fields, 1)} max_unacked={self.max_unacked}")
            for line in dump_fields(body, indent=2):
                log(line)
            return

        # Anything else: always log it in full. These should be rare
        # (focus indications, control-channel messages) -- and if the
        # display sends something we never answer (e.g. a ping request),
        # this is where it'll show up.
        log(f"<- channel={channel} msg_id={msg_id:#06x} len={len(body)}")
        for line in dump_fields(body, indent=2):
            log(line)

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
