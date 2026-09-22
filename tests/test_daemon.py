"""daemon.py is mostly integration/orchestration code, verified live
against real hardware rather than unit-tested piece by piece -- but
_disconnect_wifi_on_shutdown is a pure, testable unit with real
regression value: a user reported live (2026-09-22) that Ctrl+C left the
Pi connected to the display's WiFi, breaking the next `aa-hmi serve`
start (almost certainly the documented WiFi/Bluetooth radio-coexistence
issue on the Pi's onboard combo chip)."""
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
    holder = daemon._SessionHolder()
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

    holder = daemon._SessionHolder()
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
    holder = daemon._SessionHolder()
    holder.last_ssid = "TF811-1c64201a"

    daemon._disconnect_wifi_on_shutdown(_fake_args(), holder)  # must not raise
    captured = capsys.readouterr()
    assert "WiFi cleanup on shutdown failed" in captured.err


def test_holder_last_ssid_defaults_to_none():
    assert daemon._SessionHolder().last_ssid is None
