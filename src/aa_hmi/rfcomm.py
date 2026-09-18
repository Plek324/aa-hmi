"""rfcomm.py -- find the real AA-Wireless RFCOMM channel on a paired device.

aa_pi2display's own findings (real-protocol-findings.md) are the basis for
this module's strategy: SDP discovery for the AA Wireless service was found
completely broken on the test unit (empty response every time), while
brute-force scanning RFCOMM channels 1-30 reliably found the real service
at channel 4 -- but channels 7, 12 and 15 *also* accepted a raw connection
without being the right service. So: accepting a connection is necessary
but never sufficient. A candidate channel is only accepted once something
that looks like a real protocol reply comes back.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass

from .errors import NoChannelFoundError
from .log import log
from .retry import RetryCancelledError

MAX_CHANNEL = 30


def build_channel_candidates(mac: str, *, sdptool_probe=None) -> list[int]:
    """Pure-ish candidate ordering: SDP-suggested channels first (if any),
    then every channel 1..MAX_CHANNEL not already listed, de-duplicated,
    order preserved. sdptool_probe is injectable for testing (defaults to
    _sdp_channels_via_sdptool, which shells out for real)."""
    probe = sdptool_probe or _sdp_channels_via_sdptool
    sdp_channels = probe(mac)
    seen = set()
    ordered = []
    for ch in [*sdp_channels, *range(1, MAX_CHANNEL + 1)]:
        if ch not in seen and 1 <= ch <= MAX_CHANNEL:
            seen.add(ch)
            ordered.append(ch)
    return ordered


def _sdp_channels_via_sdptool(mac: str) -> list[int]:
    """Best-effort SDP fast-path. Never required -- any failure (missing
    tool, timeout, empty/unparseable output) just means an empty list, and
    the brute-force range below covers everything anyway."""
    if not shutil.which("sdptool"):
        return []
    try:
        out = subprocess.run(["sdptool", "browse", mac], capture_output=True,
                              text=True, timeout=15).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    channels = []
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith("channel:"):
            try:
                channels.append(int(line.split(":", 1)[1].strip()))
            except ValueError:
                continue
    return channels


@dataclass
class ChannelResult:
    channel: int
    sock: socket.socket
    discovery_method: str  # "sdp" or "bruteforce", based on candidate position


def connect_rfcomm(mac: str, channel: int, timeout: float = 3.0) -> socket.socket:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    sock.settimeout(timeout)
    sock.connect((mac, channel))
    return sock


def find_channel(mac: str, validate, *, connect_timeout: float = 3.0,
                  sdptool_probe=None, cancel_event: threading.Event | None = None) -> ChannelResult:
    """Try each candidate channel in order; for each that accepts a raw
    RFCOMM connection, call validate(sock) -> bool to check whether it
    actually speaks the expected protocol (real callers pass something
    that sends WifiInfoRequest and checks for a structurally valid
    WifiInfoResponse -- see bootstrap.py). Returns the first channel that
    both connects and validates; closes every rejected socket along the
    way. Raises NoChannelFoundError if nothing worked.

    cancel_event, if given and set, stops BETWEEN channel attempts
    (raises RetryCancelledError) -- found live to matter (2026-09-18):
    this whole function is a single ~90s-worst-case blocking call from
    retry_with_backoff's point of view (its own cancel_event is only
    checked between *entire* calls to this function, not during one), so
    without checking here too, a daemon shutdown signal received partway
    through a 30-channel scan wouldn't be noticed until the scan finished
    on its own. Does not interrupt a single channel's connect() already
    in flight (bounded by connect_timeout anyway, default 3s)."""
    candidates = build_channel_candidates(mac, sdptool_probe=sdptool_probe)
    sdp_count = len(_sdp_channels_via_sdptool(mac)) if sdptool_probe is None else 0
    for idx, channel in enumerate(candidates):
        if cancel_event is not None and cancel_event.is_set():
            raise RetryCancelledError(idx)
        log(f"  trying RFCOMM channel {channel} ({idx + 1}/{len(candidates)})...",
            verbose_only=True, verbose=True)
        try:
            sock = connect_rfcomm(mac, channel, timeout=connect_timeout)
        except OSError as exc:
            log(f"    channel {channel}: connect failed ({exc})", verbose_only=True, verbose=True)
            continue
        try:
            if validate(sock):
                method = "sdp" if idx < sdp_count else "bruteforce"
                log(f"  channel {channel} validated ({method})")
                return ChannelResult(channel=channel, sock=sock, discovery_method=method)
        except OSError as exc:
            log(f"    channel {channel}: validation failed ({exc})", verbose_only=True, verbose=True)
        sock.close()
    raise NoChannelFoundError(
        f"no RFCOMM channel on {mac} accepted a connection and spoke the "
        f"expected protocol (tried {len(candidates)} candidates, 1-{MAX_CHANNEL})"
    )
