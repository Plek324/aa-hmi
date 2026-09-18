"""orchestrate.py never had dedicated unit tests before this file -- it
was only ever exercised indirectly through cli.py's `run` command. Now
that daemon.py depends on it too, it gets real coverage of its own."""
from unittest.mock import MagicMock

import pytest

from aa_hmi import cache, orchestrate
from aa_hmi.discovery import BtDevice
from aa_hmi.errors import NoDeviceSelectedError
from aa_hmi.messages import WifiInfo
from aa_hmi.retry import RetryExhaustedError


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "aa-hmi" / "devices.json"


# --- resolve_device ---

def test_resolve_device_explicit_device_flag_skips_everything(cache_path):
    bt = MagicMock()
    args = orchestrate.DeviceSelectionArgs(device="AA:BB:CC:DD:EE:FF")
    mac, cached = orchestrate.resolve_device(args, bt, cache_path)
    assert mac == "AA:BB:CC:DD:EE:FF"
    assert cached is None
    bt.scan.assert_not_called()


def test_resolve_device_single_cached_device_auto_selected(cache_path):
    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", name="TF811BT")
    bt = MagicMock()
    args = orchestrate.DeviceSelectionArgs()
    mac, cached = orchestrate.resolve_device(args, bt, cache_path)
    assert mac == "AA:BB:CC:DD:EE:FF"
    assert cached.name == "TF811BT"
    bt.scan.assert_not_called()


def test_resolve_device_multiple_cached_non_interactive_raises(cache_path):
    cache.upsert_device(cache_path, "AA:AA:AA:AA:AA:AA", name="one")
    cache.upsert_device(cache_path, "BB:BB:BB:BB:BB:BB", name="two")
    bt = MagicMock()
    args = orchestrate.DeviceSelectionArgs(non_interactive=True)
    with pytest.raises(NoDeviceSelectedError):
        orchestrate.resolve_device(args, bt, cache_path)


def test_resolve_device_multiple_cached_interactive_pick(cache_path, monkeypatch):
    cache.upsert_device(cache_path, "AA:AA:AA:AA:AA:AA", name="one")
    cache.upsert_device(cache_path, "BB:BB:BB:BB:BB:BB", name="two")
    bt = MagicMock()
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    args = orchestrate.DeviceSelectionArgs()
    mac, cached = orchestrate.resolve_device(args, bt, cache_path)
    assert mac in ("AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB")  # dict order from cache -- just confirm it's one of them
    assert cached is not None


def test_resolve_device_no_cache_non_interactive_raises(cache_path):
    bt = MagicMock()
    args = orchestrate.DeviceSelectionArgs(non_interactive=True)
    with pytest.raises(NoDeviceSelectedError):
        orchestrate.resolve_device(args, bt, cache_path)


def test_resolve_device_no_cache_interactive_scans_and_picks(cache_path, monkeypatch):
    bt = MagicMock()
    bt.scan.return_value = [BtDevice(mac="CC:CC:CC:CC:CC:CC", name="Found Device")]
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    args = orchestrate.DeviceSelectionArgs(scan_duration=5.0)
    mac, cached = orchestrate.resolve_device(args, bt, cache_path)
    assert mac == "CC:CC:CC:CC:CC:CC"
    assert cached is None  # never cached before
    bt.scan.assert_called_once_with(duration_s=5.0)


def test_resolve_device_no_selection_raises(cache_path, monkeypatch):
    bt = MagicMock()
    bt.scan.return_value = [BtDevice(mac="CC:CC:CC:CC:CC:CC", name="Found Device")]
    monkeypatch.setattr("builtins.input", lambda prompt: "")  # blank = cancel
    args = orchestrate.DeviceSelectionArgs()
    with pytest.raises(NoDeviceSelectedError):
        orchestrate.resolve_device(args, bt, cache_path)


# --- bootstrap_wifi_info ---

_FAKE_INFO = WifiInfo(ssid="TF811-x", key="12345678", bssid="aa:bb:cc:dd:ee:ff", security_mode_raw=5,
                       access_point_type_raw=None)


def test_bootstrap_wifi_info_forced_channel(monkeypatch):
    fake_sock = MagicMock()
    monkeypatch.setattr(orchestrate, "connect_rfcomm", lambda mac, ch: fake_sock)
    monkeypatch.setattr(orchestrate, "get_wifi_info", lambda sock: _FAKE_INFO)
    info, channel, method, _sock = orchestrate.bootstrap_wifi_info("AA:BB:CC:DD:EE:FF", channel=7, cached=None, bt_timeout=5.0)
    assert channel == 7
    assert method == "forced"
    assert info is _FAKE_INFO
    fake_sock.close.assert_called_once()


def test_bootstrap_wifi_info_confirm_connected_keeps_socket_open(monkeypatch):
    """Regression test for a real integration bug found live (2026-09-18):
    the display's TCP video port refuses connections unless the RFCOMM/
    Bluetooth link is still OPEN, not just having sent the confirm
    messages on an already-closed socket. confirm_connected=True must:
    (1) call confirm_wifi_connected() on the socket, (2) NOT close it --
    it must come back open as the 4th return value for the caller
    (daemon.py) to hold open and close itself once the video session is
    established."""
    fake_sock = MagicMock()
    confirm_calls = []
    monkeypatch.setattr(orchestrate, "connect_rfcomm", lambda mac, ch: fake_sock)
    monkeypatch.setattr(orchestrate, "get_wifi_info", lambda sock: _FAKE_INFO)
    monkeypatch.setattr(orchestrate, "confirm_wifi_connected",
                         lambda sock: confirm_calls.append(sock))

    info, channel, method, returned_sock = orchestrate.bootstrap_wifi_info(
        "AA:BB:CC:DD:EE:FF", channel=7, cached=None, bt_timeout=5.0, confirm_connected=True,
    )
    assert confirm_calls == [fake_sock]
    fake_sock.close.assert_not_called()  # must NOT be closed -- this was the actual bug
    assert returned_sock is fake_sock  # caller must get the still-open socket back


def test_bootstrap_wifi_info_default_does_not_confirm_connected_and_closes_socket(monkeypatch):
    """`aa-hmi run`'s own scope never needs confirm_connected -- must stay
    opt-in -- and, unlike the confirm_connected=True path, must keep
    closing the socket immediately (returning None for it) exactly like
    before this feature existed."""
    fake_sock = MagicMock()
    monkeypatch.setattr(orchestrate, "connect_rfcomm", lambda mac, ch: fake_sock)
    monkeypatch.setattr(orchestrate, "get_wifi_info", lambda sock: _FAKE_INFO)
    confirm_calls = []
    monkeypatch.setattr(orchestrate, "confirm_wifi_connected", lambda sock: confirm_calls.append(1))

    info, channel, method, returned_sock = orchestrate.bootstrap_wifi_info(
        "AA:BB:CC:DD:EE:FF", channel=7, cached=None, bt_timeout=5.0,
    )
    assert confirm_calls == []
    fake_sock.close.assert_called_once()
    assert returned_sock is None


def test_bootstrap_wifi_info_uses_cached_channel_when_it_works(monkeypatch):
    fake_sock = MagicMock()
    monkeypatch.setattr(orchestrate, "connect_rfcomm", lambda mac, ch: fake_sock)
    monkeypatch.setattr(orchestrate, "get_wifi_info", lambda sock: _FAKE_INFO)
    cached = cache.DeviceRecord(mac="AA:BB:CC:DD:EE:FF", rfcomm_channel=4, discovery_method="bruteforce")
    info, channel, method, _sock = orchestrate.bootstrap_wifi_info("AA:BB:CC:DD:EE:FF", channel=None, cached=cached, bt_timeout=5.0)
    assert channel == 4
    assert method == "bruteforce"


def test_bootstrap_wifi_info_falls_back_to_find_channel_when_cache_fails(monkeypatch):
    def fake_connect(mac, ch):
        raise OSError("connection refused")

    fake_result = MagicMock(channel=9, discovery_method="bruteforce", sock=MagicMock())
    monkeypatch.setattr(orchestrate, "connect_rfcomm", fake_connect)
    monkeypatch.setattr(orchestrate, "find_channel", lambda mac, validate, **kw: fake_result)
    monkeypatch.setattr(orchestrate, "get_wifi_info", lambda sock: _FAKE_INFO)
    cached = cache.DeviceRecord(mac="AA:BB:CC:DD:EE:FF", rfcomm_channel=4, discovery_method="bruteforce")
    info, channel, method, _sock = orchestrate.bootstrap_wifi_info("AA:BB:CC:DD:EE:FF", channel=None, cached=cached, bt_timeout=5.0)
    assert channel == 9


def test_bootstrap_wifi_info_exhausted_retries_exits_with_hint(monkeypatch, capsys):
    def always_fails():
        raise OSError("nope")

    def fake_retry(fn, **kwargs):
        raise RetryExhaustedError(attempts=5, last_exc=OSError("nope"))

    monkeypatch.setattr(orchestrate, "retry_with_backoff", fake_retry)
    with pytest.raises(SystemExit):
        orchestrate.bootstrap_wifi_info("AA:BB:CC:DD:EE:FF", channel=None, cached=None, bt_timeout=5.0)
    captured = capsys.readouterr()
    assert "Bluetooth" in captured.err
