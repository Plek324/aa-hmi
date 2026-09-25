"""daemon.py is mostly integration/orchestration code, verified live
against real hardware rather than unit-tested piece by piece -- but
_disconnect_wifi_on_shutdown is a pure, testable unit with real
regression value: a user reported live (2026-09-22) that Ctrl+C left the
Pi connected to the display's WiFi, breaking the next `aa-hmi serve`
start (almost certainly the documented WiFi/Bluetooth radio-coexistence
issue on the Pi's onboard combo chip)."""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from aa_hmi import daemon, wifi


def _fake_args(wifi_iface="wlan0"):
    return SimpleNamespace(wifi_iface=wifi_iface)


def test_disconnect_wifi_on_shutdown_does_nothing_if_never_connected(monkeypatch):
    """No bootstrap ever succeeded (e.g. daemon killed before first
    connect) -- nothing to clean up, and must not call nmcli at all."""
    disconnect_calls = []
    monkeypatch.setattr(wifi, "disconnect", lambda iface=None: disconnect_calls.append(iface))
    holder = daemon._SessionHolder(encoder_mode="per-image")
    assert holder.last_ssid is None

    daemon._disconnect_wifi_on_shutdown(_fake_args(), holder)
    assert disconnect_calls == []


def test_disconnect_wifi_on_shutdown_disconnects_and_forgets(monkeypatch):
    """The actual fix: once a device has connected, final shutdown must
    disconnect the interface AND forget the stale connection profile --
    mirrors aa_pi2display's run_session.sh, which always did both."""
    disconnect_calls = []
    forget_calls = []
    monkeypatch.setattr(wifi, "disconnect", lambda iface=None: disconnect_calls.append(iface))
    monkeypatch.setattr(wifi, "forget", lambda ssid: forget_calls.append(ssid))

    holder = daemon._SessionHolder(encoder_mode="per-image")
    holder.last_ssid = "TF811-1c64201a"

    daemon._disconnect_wifi_on_shutdown(_fake_args(wifi_iface="wlan0"), holder)

    assert disconnect_calls == ["wlan0"]
    assert forget_calls == ["TF811-1c64201a"]


def test_disconnect_wifi_on_shutdown_never_raises(monkeypatch, capsys):
    """Best-effort cleanup -- a failure here must be logged, never allowed
    to propagate and mask (or replace) a real shutdown."""
    def boom(iface=None):
        raise OSError("nmcli exploded")

    monkeypatch.setattr(wifi, "disconnect", boom)
    holder = daemon._SessionHolder(encoder_mode="per-image")
    holder.last_ssid = "TF811-1c64201a"

    daemon._disconnect_wifi_on_shutdown(_fake_args(), holder)  # must not raise
    captured = capsys.readouterr()
    assert "WiFi cleanup on shutdown failed" in captured.err


def test_holder_last_ssid_defaults_to_none():
    assert daemon._SessionHolder().last_ssid is None


# --- _wait_until_dropped_or_stopped: liveness-timeout reconnect trigger ---
#
# Regression tests for a real bug reported live (2026-09-22): "aa-hmi
# keeps printing sent frame messages, totally unaware something is off"
# -- the display's video decoder wedged silently while the transport
# stayed healthy, and the daemon had no way to notice. These test the
# orchestration logic (when does it decide to return and trigger a
# reconnect) against a mocked VideoSession, not the real liveness
# tracking itself (that's video_session.py's own is_alive(), covered in
# test_video_session.py).

def _fake_session(*, alive_with_timeout: bool, alive_without_timeout: bool = True, seconds_idle: float = 999.0):
    session = MagicMock()
    session.is_alive.side_effect = lambda liveness_timeout=None: (
        alive_with_timeout if liveness_timeout is not None else alive_without_timeout
    )
    session.seconds_since_last_activity.return_value = seconds_idle
    return session


def test_wait_returns_when_liveness_timeout_trips_even_though_transport_is_fine():
    """The actual regression case: transport-level is_alive() is True,
    but the display has gone quiet longer than the timeout -- must still
    return (triggering a reconnect), not loop forever."""
    session = _fake_session(alive_with_timeout=False, alive_without_timeout=True)
    stop_event = threading.Event()

    daemon._wait_until_dropped_or_stopped(session, stop_event, liveness_timeout=60.0, poll_interval=0.01)

    assert not stop_event.is_set()  # returned because of the session, not because we were told to stop


def test_wait_keeps_looping_while_alive_with_timeout_until_stopped():
    session = _fake_session(alive_with_timeout=True)
    stop_event = threading.Event()
    threading.Timer(0.05, stop_event.set).start()

    daemon._wait_until_dropped_or_stopped(session, stop_event, liveness_timeout=60.0, poll_interval=0.01)

    assert stop_event.is_set()  # returned because we were told to stop, not a false reconnect


def test_wait_with_liveness_timeout_none_behaves_like_transport_only_check():
    """liveness_timeout=None (the --liveness-timeout 0 case) must not
    trigger on a stale-but-otherwise-fine session -- only real transport
    death should end the wait."""
    session = _fake_session(alive_with_timeout=True, alive_without_timeout=True)
    stop_event = threading.Event()
    threading.Timer(0.05, stop_event.set).start()

    daemon._wait_until_dropped_or_stopped(session, stop_event, liveness_timeout=None, poll_interval=0.01)

    assert stop_event.is_set()


# --- video timestamps ---
#
# Every message of one image gets the same timestamp: real microseconds
# since the session opened.

class _RecordingSession:
    def __init__(self):
        self.sent = []

    def is_alive(self, **kw):
        return True

    def send_frame(self, nal_bytes, ts):
        self.sent.append(ts)
        return 1


def _two_messages(monkeypatch):
    monkeypatch.setattr(daemon.encoder, "encode_frame_to_access_units",
                         lambda data, w, h, **kw: [[b"\x67", b"\x65"], [b"\x41"]])


def _frame():
    return SimpleNamespace(pixel_data=b"", width=854, height=480)


def test_all_messages_of_one_image_share_a_timestamp(monkeypatch):
    _two_messages(monkeypatch)
    holder = daemon._SessionHolder(encoder_mode="per-image")
    holder.session = _RecordingSession()
    holder.session_start = daemon.time.monotonic()
    holder.on_frame(_frame())
    assert len(set(holder.session.sent)) == 1
    assert holder.image_counter == 1
    assert holder.frame_counter == 2


def test_timestamp_tracks_real_time(monkeypatch):
    _two_messages(monkeypatch)
    holder = daemon._SessionHolder(encoder_mode="per-image")
    holder.session = _RecordingSession()
    holder.session_start = daemon.time.monotonic() - 10.0  # session opened 10s ago
    holder.on_frame(_frame())
    assert 9_900_000 < holder.session.sent[0] < 11_000_000


def test_persistent_encoder_failure_falls_back_to_per_image(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.encoder, "encode_frame_to_access_units",
                         lambda data, w, h, **kw: calls.append("per-image") or [[b"\x65"]])
    holder = daemon._SessionHolder(encoder_mode="persistent")

    def broken(*a):
        raise daemon.EncoderError("ffmpeg died")
    holder.persistent_encoder.encode = broken
    holder.session = _RecordingSession()
    holder.on_frame(_frame())
    assert calls == ["per-image"]
    assert len(holder.session.sent) == 1


# --- touch: decoded and mapped to client coordinates ---

def test_client_touch_maps_display_coordinates_to_the_client_image():
    from aa_hmi.protocol import encode_varint_field
    location = encode_varint_field(1, 409) + encode_varint_field(2, 240) + encode_varint_field(3, 0)
    touch = bytes([0x0A, len(location)]) + location + encode_varint_field(3, 0)
    raw = encode_varint_field(1, 1) + bytes([0x1A, len(touch)]) + touch

    holder = daemon._SessionHolder(encoder_mode="per-image", pad=(800, 480, 9, 20))
    holder.video_size = (800, 480)
    event = holder.client_touch(raw)
    assert (event.x, event.y, event.action.name) == (400, 220, "PRESS")


# --- keep-alive: resend the last image when a program goes quiet ---

class _KeepaliveSession(_RecordingSession):
    def __init__(self, seconds_since_send):
        super().__init__()
        self.seconds_since_send = seconds_since_send

    def ack_stats(self):
        return {"seconds_since_send": self.seconds_since_send}


def _keepalive_holder(monkeypatch, seconds_since_send, keepalive=10.0):
    encoded = []
    monkeypatch.setattr(daemon.encoder, "encode_frame_to_access_units",
                         lambda data, w, h, **kw: encoded.append(data) or [[b"\x65"]])
    holder = daemon._SessionHolder(encoder_mode="per-image")
    holder.session = _KeepaliveSession(seconds_since_send)
    holder.keepalive = keepalive
    holder.client_size = (4, 2)
    return holder, encoded


def test_keepalive_resends_the_last_image_after_the_interval(monkeypatch):
    holder, encoded = _keepalive_holder(monkeypatch, seconds_since_send=11.0)
    holder.last_frame = SimpleNamespace(pixel_data=b"last image", width=4, height=2)
    holder.keepalive_tick()
    assert encoded == [b"last image"]


def test_keepalive_does_nothing_while_images_keep_coming(monkeypatch):
    holder, encoded = _keepalive_holder(monkeypatch, seconds_since_send=3.0)
    holder.last_frame = SimpleNamespace(pixel_data=b"last image", width=4, height=2)
    holder.keepalive_tick()
    assert encoded == []


def test_keepalive_sends_at_once_on_a_fresh_session(monkeypatch):
    """Nothing sent yet on this session (e.g. right after a reconnect):
    the display gets the last image back immediately."""
    holder, encoded = _keepalive_holder(monkeypatch, seconds_since_send=None)
    holder.last_frame = SimpleNamespace(pixel_data=b"last image", width=4, height=2)
    holder.keepalive_tick()
    assert encoded == [b"last image"]


def test_keepalive_uses_a_placeholder_before_any_program_sent_an_image(monkeypatch):
    holder, encoded = _keepalive_holder(monkeypatch, seconds_since_send=None)
    holder.keepalive_tick()
    assert len(encoded) == 1 and len(encoded[0]) == 4 * 2 * 3


def test_keepalive_disabled(monkeypatch):
    holder, encoded = _keepalive_holder(monkeypatch, seconds_since_send=None, keepalive=None)
    holder.keepalive_tick()
    assert encoded == []


def test_frames_arriving_while_disconnected_are_kept_for_later(monkeypatch):
    holder, encoded = _keepalive_holder(monkeypatch, seconds_since_send=None)
    holder.session = None
    holder.on_frame(SimpleNamespace(pixel_data=b"while away", width=4, height=2))
    assert encoded == [] and holder.last_frame.pixel_data == b"while away"
