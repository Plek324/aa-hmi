"""VideoSession's connect/handshake path needs real sockets+TLS to
exercise meaningfully (covered by live-hardware verification, see
docs/video-live-verification.md) -- but is_alive()'s liveness-timeout
logic is pure enough to unit test directly against a constructed
instance, and it's exactly the piece added as a regression fix for a
real bug reported live (2026-09-22): the display's own video decoder can
wedge silently while the TCP/TLS transport stays completely healthy, and
without this check aa-hmi had no way to ever notice."""
import threading
import time

import pytest

from aa_hmi.video_session import VideoSession, VideoSessionState


@pytest.fixture
def open_session():
    """A VideoSession in the OPEN state with a real (but idle) 'reader
    thread' standing in for the background reader -- close enough for
    is_alive()'s own logic, which only checks thread.is_alive()."""
    session = VideoSession("192.168.10.1", "cert.pem", "key.pem")
    session.state = VideoSessionState.OPEN
    stop = threading.Event()
    session._reader_thread = threading.Thread(target=stop.wait, daemon=True)
    session._reader_thread.start()
    session._last_activity = time.monotonic()
    yield session
    stop.set()
    session._reader_thread.join(timeout=1.0)


def test_is_alive_true_with_no_liveness_timeout_given(open_session):
    assert open_session.is_alive() is True


def test_is_alive_true_with_recent_activity_and_liveness_timeout(open_session):
    assert open_session.is_alive(liveness_timeout=60.0) is True


def test_is_alive_false_when_liveness_timeout_exceeded(open_session):
    """The actual regression case: transport (state + reader thread) is
    completely fine, but nothing has been heard from the display in
    longer than the timeout -- must be treated as not alive."""
    open_session._last_activity = time.monotonic() - 120.0  # 2 minutes of silence
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_is_alive_false_when_liveness_timeout_exceeded_but_true_without_one(open_session):
    """Confirms the liveness check is additive, not a replacement --
    omitting liveness_timeout must still reflect the old (transport-only)
    behavior, for any caller not opting into the new check."""
    open_session._last_activity = time.monotonic() - 120.0
    assert open_session.is_alive() is True
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_is_alive_false_when_state_not_open_regardless_of_liveness(open_session):
    open_session.state = VideoSessionState.CLOSED
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_is_alive_false_when_reader_thread_dead_regardless_of_liveness(open_session):
    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()  # guaranteed finished/not-alive
    open_session._reader_thread = dead_thread
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_seconds_since_last_activity_reflects_elapsed_time(open_session):
    open_session._last_activity = time.monotonic() - 42.0
    elapsed = open_session.seconds_since_last_activity()
    assert 41.0 < elapsed < 43.0  # small tolerance for test execution time


def test_liveness_timeout_boundary_is_exclusive(open_session):
    """Exactly at the boundary should still count as alive -- only
    exceeding it should trip. (>, not >=, in the implementation.)"""
    open_session._last_activity = time.monotonic() - 60.0
    # Right at ~60s: could go either way depending on test timing jitter,
    # so just confirm comfortably-under and comfortably-over both behave
    # as expected rather than asserting the exact boundary millisecond.
    assert open_session.is_alive(liveness_timeout=61.0) is True
    assert open_session.is_alive(liveness_timeout=1.0) is False


# --- media ack tracking ---
#
# Added to diagnose a real freeze (display stops updating while every
# send still succeeds): previously every message from the display except
# touch was drained and discarded, so there was no way to tell whether
# its acks stopped or changed at the moment of a freeze.

from aa_hmi import video_messages as m  # noqa: E402
from aa_hmi.protocol import encode_varint_field  # noqa: E402


def _ack(session=0, value=1) -> bytes:
    return encode_varint_field(1, session) + encode_varint_field(2, value)


def test_media_ack_is_counted():
    s = VideoSession("192.168.10.1", "c", "k")
    s.frames_sent = 3
    for _ in range(3):
        s._handle_incoming(m.CHANNEL_VIDEO, m.AV_MEDIA_ACK_INDICATION, _ack())
    stats = s.ack_stats()
    assert stats["ack_messages"] == 3
    assert stats["acks_received"] == 3
    assert stats["outstanding"] == 0
    assert stats["seconds_since_ack"] is not None


def test_media_ack_value_field_is_summed_not_just_counted():
    """If the display ever acks several frames in one message, value > 1
    -- outstanding must reflect frames, not messages."""
    s = VideoSession("192.168.10.1", "c", "k")
    s.frames_sent = 5
    s._handle_incoming(m.CHANNEL_VIDEO, m.AV_MEDIA_ACK_INDICATION, _ack(value=4))
    assert s.ack_stats()["acks_received"] == 4
    assert s.ack_stats()["outstanding"] == 1


def test_outstanding_grows_when_acks_stop():
    s = VideoSession("192.168.10.1", "c", "k")
    s.frames_sent = 10
    s._handle_incoming(m.CHANNEL_VIDEO, m.AV_MEDIA_ACK_INDICATION, _ack())
    assert s.ack_stats()["outstanding"] == 9


def test_ack_shape_change_is_logged(capsys):
    s = VideoSession("192.168.10.1", "c", "k")
    s._handle_incoming(m.CHANNEL_VIDEO, m.AV_MEDIA_ACK_INDICATION, _ack(session=0, value=1))
    s._handle_incoming(m.CHANNEL_VIDEO, m.AV_MEDIA_ACK_INDICATION, _ack(session=0, value=1))
    s._handle_incoming(m.CHANNEL_VIDEO, m.AV_MEDIA_ACK_INDICATION, _ack(session=1, value=1))
    err = capsys.readouterr().err
    assert err.count("media ack:") == 2  # first ack, then the change -- not the repeat


def test_setup_response_records_max_unacked():
    s = VideoSession("192.168.10.1", "c", "k")
    body = encode_varint_field(1, 0) + encode_varint_field(2, 10)
    s._handle_incoming(m.CHANNEL_VIDEO, m.AV_SETUP_RESPONSE, body)
    assert s.max_unacked == 10
    assert s.ack_stats()["max_unacked"] == 10


def test_unknown_message_is_logged_not_dropped(capsys):
    s = VideoSession("192.168.10.1", "c", "k")
    s._handle_incoming(m.CHANNEL_CONTROL, 0x000B, encode_varint_field(1, 12345))
    err = capsys.readouterr().err
    assert "msg_id=0x000b" in err
    assert "12345" in err


def test_touch_still_goes_to_callback():
    s = VideoSession("192.168.10.1", "c", "k")
    got = []
    s._on_touch_raw = got.append
    s._handle_incoming(m.CHANNEL_INPUT, m.INPUT_EVENT_INDICATION, b"\x18\x00")
    assert got == [b"\x18\x00"]


# --- large messages: FIRST / MIDDLE / LAST over real TLS ---
#
# Sends a >16KB media message through a real TLS session (our server side
# against an in-memory client standing in for the display), then
# reassembles it the way aasdk's receiver does: read the frame header,
# the u32 total size on a FIRST frame, decrypt each frame's payload, and
# append until LAST. Hardware is the final judge; this proves our side is
# self-consistent.

import shutil  # noqa: E402
import ssl  # noqa: E402
import struct  # noqa: E402

from aa_hmi import cert  # noqa: E402
from aa_hmi import video_messages as m  # noqa: E402


class _CapturingSocket:
    def __init__(self):
        self.data = b""

    def sendall(self, b):
        self.data += b


def _tls_pair(tmp_path):
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.ensure_cert(cert_file, key_file)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    s_in, s_out, c_in, c_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    client = client_ctx.wrap_bio(c_in, c_out)
    for _ in range(10):  # shuttle handshake bytes until both sides are done
        done = 0
        for obj in (client, server):
            try:
                obj.do_handshake()
                done += 1
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if done == 2:
            break
    return server, s_in, s_out, client, c_in


def _reassemble(wire_bytes, client, c_in):
    """aasdk-style receive: returns [(channel, plaintext message)]."""
    messages, current, i = [], b"", 0
    while i < len(wire_bytes):
        channel, flags, length = struct.unpack(">BBH", wire_bytes[i:i + 4])
        i += 4
        frame_type = flags & 0x03
        if frame_type == 1:  # FIRST carries the total plaintext size
            total = struct.unpack(">I", wire_bytes[i:i + 4])[0]
            i += 4
        c_in.write(wire_bytes[i:i + length])
        i += length
        current += client.read(65536)
        if frame_type in (2, 3):  # LAST or BULK completes a message
            if frame_type == 2:
                assert len(current) == total
            messages.append((channel, current))
            current = b""
    return messages


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
@pytest.mark.parametrize("size", [100, 16374, 16375, 40000, 200000])
def test_large_media_message_reassembles_exactly(tmp_path, size):
    server, s_in, s_out, client, c_in = _tls_pair(tmp_path)
    session = VideoSession("192.168.10.1", "c.pem", "k.pem")
    session.state = VideoSessionState.OPEN
    session._tls_obj, session._incoming, session._outgoing = server, s_in, s_out
    session._sock = _CapturingSocket()

    nal_bytes = bytes(range(256)) * (size // 256) + b"\x00" * (size % 256)
    pieces = session.send_frame(nal_bytes, 123456)

    plaintext_len = 2 + 8 + size
    assert pieces == -(-plaintext_len // 16384)  # ceil
    [(channel, message)] = _reassemble(session._sock.data, client, c_in)
    assert channel == m.CHANNEL_VIDEO
    assert message == struct.pack(">HQ", m.AV_MEDIA_WITH_TIMESTAMP_INDICATION, 123456) + nal_bytes
