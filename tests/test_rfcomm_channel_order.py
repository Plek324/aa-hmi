import threading

import pytest

from aa_hmi import rfcomm
from aa_hmi.errors import NoChannelFoundError
from aa_hmi.retry import RetryCancelledError
from aa_hmi.rfcomm import MAX_CHANNEL, build_channel_candidates, find_channel


def test_no_sdp_hits_falls_back_to_full_brute_force_range():
    candidates = build_channel_candidates("AA:BB:CC:DD:EE:FF", sdptool_probe=lambda mac: [])
    assert candidates == list(range(1, MAX_CHANNEL + 1))


def test_sdp_hits_come_first_deduplicated():
    candidates = build_channel_candidates("AA:BB:CC:DD:EE:FF", sdptool_probe=lambda mac: [4, 7])
    assert candidates[:2] == [4, 7]
    assert candidates.count(4) == 1
    assert candidates.count(7) == 1
    assert len(candidates) == MAX_CHANNEL  # still covers every channel, just reordered
    assert set(candidates) == set(range(1, MAX_CHANNEL + 1))


def test_sdp_hits_out_of_valid_range_are_dropped():
    candidates = build_channel_candidates("AA:BB:CC:DD:EE:FF", sdptool_probe=lambda mac: [0, 99, 4])
    assert candidates[0] == 4
    assert 0 not in candidates
    assert 99 not in candidates
    assert len(candidates) == MAX_CHANNEL


# --- find_channel cancellation ---
#
# Regression tests for a real daemon shutdown-latency bug found live
# (2026-09-18): find_channel is a single, ~90s-worst-case blocking call
# from retry_with_backoff's point of view -- its own cancel_event is only
# checked BETWEEN calls to this whole function, not during one -- so
# without a cancel_event check inside the per-channel loop too, a daemon
# shutdown signal received partway through a 30-channel scan went
# unnoticed until the scan finished on its own.

def _always_fails_to_connect(mac, channel, timeout=3.0):
    raise OSError("connection refused")


def test_find_channel_without_cancel_event_scans_everything(monkeypatch):
    monkeypatch.setattr(rfcomm, "connect_rfcomm", _always_fails_to_connect)
    with pytest.raises(NoChannelFoundError):
        find_channel("AA:BB:CC:DD:EE:FF", lambda sock: False,
                      sdptool_probe=lambda mac: [])
    # (implicitly: reaching NoChannelFoundError means it tried all 30 --
    # no assertion needed beyond not raising something else)


def test_find_channel_stops_early_when_cancelled_mid_scan(monkeypatch):
    attempted = []

    def fake_connect(mac, channel, timeout=3.0):
        attempted.append(channel)
        if len(attempted) == 3:
            cancel_event.set()  # simulates a shutdown signal firing partway through
        raise OSError("connection refused")

    monkeypatch.setattr(rfcomm, "connect_rfcomm", fake_connect)
    cancel_event = threading.Event()

    with pytest.raises(RetryCancelledError):
        find_channel("AA:BB:CC:DD:EE:FF", lambda sock: False,
                      sdptool_probe=lambda mac: [], cancel_event=cancel_event)
    # Must have stopped at channel 4 (the check happens at the TOP of the
    # loop, before attempting the next channel) -- not scanned all 30.
    assert attempted == [1, 2, 3]


def test_find_channel_already_cancelled_tries_nothing(monkeypatch):
    attempted = []
    monkeypatch.setattr(rfcomm, "connect_rfcomm", lambda mac, ch, timeout=3.0: attempted.append(ch))
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(RetryCancelledError):
        find_channel("AA:BB:CC:DD:EE:FF", lambda sock: False,
                      sdptool_probe=lambda mac: [], cancel_event=cancel_event)
    assert attempted == []
