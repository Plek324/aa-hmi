"""wifi.py -- join a WiFi network via nmcli.

Directly modeled on aa_pi2display's run_session.sh, which found (the hard
way) that NetworkManager can leave behind a connection profile for a given
SSID that's missing required security settings (e.g. from a prior
Bluetooth-bootstrap tool creating one as a side effect) -- `nmcli device
wifi connect` then silently reuses that broken profile instead of
negotiating a fresh one and fails with "property is missing". The fix
there (and here) is to always delete any pre-existing profile by that
name before connecting.

Unlike run_session.sh's best-effort `|| true` shell style, every function
here raises a typed NmcliError on real failure -- this tool needs to
report success/failure accurately to its caller (an interactive user, or
a future non-interactive/systemd context), not just best-effort.
"""
from __future__ import annotations

import subprocess
import time

from .errors import NmcliError
from .log import log


def _run(args: list[str], *, check: bool = True, timeout: float = 20.0) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise NmcliError(["nmcli", *args], -1,
                          "nmcli not found -- is NetworkManager installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise NmcliError(["nmcli", *args], -1, f"timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise NmcliError(["nmcli", *args], proc.returncode, proc.stderr)
    return proc


def find_wifi_iface() -> str | None:
    """First device of type "wifi" known to NetworkManager, or None."""
    proc = _run(["-t", "-g", "DEVICE,TYPE", "device"], check=False)
    for line in proc.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return None


def delete_stale_profile(ssid: str) -> None:
    """Delete any existing connection profile named exactly `ssid`, if
    present. Swallows only "no profile to delete" failures -- any other
    failure propagates, since a permissions error here would otherwise
    surface later as a much more confusing connect failure.

    Confirmed live (2026-09-18, nmcli on Raspberry Pi OS / Debian
    Trixie) that the real error text is "unknown connection", not "no
    such connection" as originally guessed -- both phrasings are
    accepted here in case wording varies by nmcli version."""
    proc = _run(["connection", "delete", ssid], check=False)
    if proc.returncode != 0:
        stderr_lower = proc.stderr.lower()
        if "unknown connection" not in stderr_lower and "no such connection" not in stderr_lower:
            raise NmcliError(["nmcli", "connection", "delete", ssid], proc.returncode, proc.stderr)


def connect(ssid: str, password: str | None = None, *, iface: str | None = None,
            timeout: float = 30.0, poll_interval: float = 1.0) -> str:
    """Join `ssid` (with `password`, or open if falsy/None). Returns the
    IPv4 address once a DHCP lease is confirmed. Raises NmcliError on any
    nmcli failure, or TimeoutError if no lease appears within `timeout`s."""
    delete_stale_profile(ssid)
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    if iface:
        args += ["ifname", iface]
    log(f"  nmcli device wifi connect {ssid!r} ({'with password' if password else 'open'})...")
    _run(args, timeout=timeout)

    target_iface = iface or find_wifi_iface()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ip = _current_ip(target_iface)
        if ip:
            log(f"  connected, IP={ip}")
            return ip
        time.sleep(poll_interval)
    raise TimeoutError(f"connected to {ssid!r} but no DHCP lease appeared within {timeout}s")


def _current_ip(iface: str | None) -> str | None:
    args = ["-t", "-g", "IP4.ADDRESS"]
    args += ["device", "show", iface] if iface else ["device", "show"]
    proc = _run(args, check=False)
    first_line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    return first_line.split("/")[0] if first_line else None


def disconnect(iface: str | None = None) -> None:
    target = iface or find_wifi_iface()
    if target:
        _run(["device", "disconnect", target], check=False)


def forget(ssid: str) -> None:
    delete_stale_profile(ssid)
