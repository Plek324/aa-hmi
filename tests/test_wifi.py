"""wifi.py's delete_stale_profile parses real nmcli stderr text to decide
"nothing to delete, fine" vs. "real failure, propagate". Guard the exact
wording with a regression test -- this was wrong once already (guessed
"no such connection", the real nmcli output says "unknown connection";
found via a live run against real hardware, 2026-09-18)."""
import subprocess

import pytest

from aa_hmi import wifi
from aa_hmi.errors import NmcliError


def _fake_completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["nmcli"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_delete_stale_profile_swallows_real_unknown_connection_wording(monkeypatch):
    # Real nmcli output (confirmed live against a Raspberry Pi's nmcli):
    real_stderr = (
        "Error: unknown connection 'TF811-1c64201a'.\n"
        "Error: cannot delete unknown connection(s): 'TF811-1c64201a'.\n"
    )
    monkeypatch.setattr(wifi, "_run", lambda args, check=False, timeout=20.0: _fake_completed(10, stderr=real_stderr))
    wifi.delete_stale_profile("TF811-1c64201a")  # must not raise


def test_delete_stale_profile_also_swallows_no_such_connection_wording(monkeypatch):
    # Older/other nmcli versions may phrase it differently -- both are accepted.
    monkeypatch.setattr(wifi, "_run", lambda args, check=False, timeout=20.0:
                         _fake_completed(10, stderr="Error: No such connection profile."))
    wifi.delete_stale_profile("SomeSSID")  # must not raise


def test_delete_stale_profile_raises_on_a_real_unrelated_failure(monkeypatch):
    monkeypatch.setattr(wifi, "_run", lambda args, check=False, timeout=20.0:
                         _fake_completed(1, stderr="Error: Permission denied"))
    with pytest.raises(NmcliError):
        wifi.delete_stale_profile("SomeSSID")


def test_delete_stale_profile_succeeds_silently_on_returncode_0(monkeypatch):
    monkeypatch.setattr(wifi, "_run", lambda args, check=False, timeout=20.0: _fake_completed(0))
    wifi.delete_stale_profile("SomeSSID")  # must not raise
