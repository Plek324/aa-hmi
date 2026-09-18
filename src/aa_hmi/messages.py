"""messages.py -- AA-Wireless Bluetooth bootstrap message IDs, and the
typed WifiInfo result decoded from a real WifiInfoResponse.

Message IDs are confirmed from aa-proxy-rs's public source
(https://github.com/aa-proxy/aa-proxy-rs, src/bluetooth.rs). The
WifiInfoResponse proto schema is confirmed from that same project's
src/protos/WifiInfoResponse.proto. See docs/protocol-notes.md for the full
writeup and the aa_pi2display sibling project's extracting-wifi-credentials.md
for the original decoded capture this was cross-checked against.

CONFIRMED (2026-09-18, captured against a real TF811BT display via
aa-proxy-rs's debug=true probe-mode logging -- full raw log in
tests/fixtures/ground_truth_probe_session.txt, procedure in
docs/capturing-ground-truth.md). The real bootstrap sequence:

  1. RFCOMM connects.
  2. HU -> POC: WifiVersionRequest (id=4), sent UNPROMPTED by the head
     unit before anything is requested. No response is required -- the
     real aa-proxy-rs probe doesn't send a WifiVersionResponse either, it
     just reads this one frame and moves on. bootstrap.py drains and
     discards it (or whatever the HU sends first, if anything).
  3. POC -> HU: WifiInfoRequest (id=2), CONFIRMED EMPTY body (len=0) --
     this was a guess before, now verified byte-for-byte from the real
     outbound frame log.
  4. HU -> POC: WifiInfoResponse (id=3) -- the credentials. Notably,
     aa-proxy-rs's OWN strict protobuf parser fails on this exact real
     response ("Message `WifiInfoResponse` is missing required fields")
     -- direct, independent confirmation that lenient parsing here (see
     parse_wifi_info_response below) is the right call, not a shortcut.

  What follows in a real session (WifiStartResponse id=7, WifiConnectStatus
  id=6) is the head unit being told "I'm now connected" -- out of this
  tool's scope (see docs/protocol-notes.md); aa-hmi stops at step 4.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from . import protocol


class MessageId(IntEnum):
    WIFI_START_REQUEST = 1
    WIFI_INFO_REQUEST = 2
    WIFI_INFO_RESPONSE = 3
    WIFI_VERSION_REQUEST = 4
    WIFI_VERSION_RESPONSE = 5
    WIFI_CONNECT_STATUS = 6
    WIFI_START_RESPONSE = 7
    WIFI_PING_REQUEST = 8
    WIFI_PING_RESPONSE = 9
    WIFI_SETUP_INFO = 11


# --- ground truth, confirmed by Task 0 (docs/capturing-ground-truth.md) ---
#
# Confirmed 2026-09-18 against a real TF811BT display -- see the class
# docstring above and tests/fixtures/ground_truth_probe_session.txt for
# the raw capture this was decoded from.
GROUND_TRUTH_CONFIRMED = True

WIFI_INFO_REQUEST_PAYLOAD: bytes = b""  # confirmed empty -- real captured frame was len=0
# WifiVersionRequest is sent BY the head unit, unprompted -- we never send
# one ourselves, so there's no "payload to send" for it. Kept as a named
# None (rather than removed) so it's easy to find if a future capture
# against different hardware turns out to need an explicit
# WifiVersionResponse reply after all.
WIFI_VERSION_REQUEST_PAYLOAD: bytes | None = None

# Confirmed real bytes from the same captured aa-proxy-rs probe session
# (tests/fixtures/ground_truth_probe_session.txt), sent immediately after
# WifiInfoResponse in every real probe run observed. `aa-hmi run`'s own
# scope never needed these (see bootstrap.py's module docstring), but
# aa-hmi serve (daemon.py) does: discovered live that without sending
# these, the display's TCP video-session port (29880) refuses connections
# even after WiFi is successfully joined -- these two messages appear to
# be what actually arms the video listener, not just receiving
# WifiInfoResponse. See bootstrap.confirm_wifi_connected() and
# docs/video-protocol-notes.md.
#   WifiStartResponse payload 0x1800 decodes as field 3 (varint) = 0.
#   WifiConnectStatus payload 0x0800 decodes as field 1 (varint) = 0.
# Neither field's real meaning is confirmed beyond "these exact bytes are
# what a real working probe sends" -- ported as opaque confirmed bytes,
# not re-derived.
WIFI_START_RESPONSE_PAYLOAD = bytes.fromhex("1800")
WIFI_CONNECT_STATUS_PAYLOAD = bytes.fromhex("0800")


class SecurityMode(IntEnum):
    """Values per the public .proto. NEVER used to validate/reject an
    incoming response -- real devices send out-of-range values (a real
    TF811BT capture had security_mode=5, which appears nowhere below)."""
    UNKNOWN_SECURITY_MODE = 0
    OPEN = 1
    WEP_64 = 2
    WEP_128 = 3
    WPA_PERSONAL = 4
    WPA2_PERSONAL = 8
    WPA_WPA2_PERSONAL = 12
    WPA_ENTERPRISE = 20
    WPA2_ENTERPRISE = 24
    WPA_WPA2_ENTERPRISE = 28


class AccessPointType(IntEnum):
    STATIC = 0
    DYNAMIC = 1


@dataclass
class WifiInfo:
    ssid: str
    key: str
    bssid: str | None
    security_mode_raw: int | None
    access_point_type_raw: int | None

    @property
    def is_open(self) -> bool:
        """True if this looks like an open (no-password) network. Driven
        by the actual key value, not the security_mode enum -- that enum
        is already known-unreliable on real hardware (see class docstring
        on SecurityMode)."""
        return not self.key


def parse_wifi_info_response(payload: bytes) -> WifiInfo:
    """Decode a WifiInfoResponse payload leniently. Raises ValueError only
    if ssid or key (the two fields this tool actually depends on) are
    missing -- every other field is optional in practice, matching real
    captured traffic."""
    fields = protocol.decode_fields(payload)
    ssid = protocol.get_string(fields, 1)
    key = protocol.get_string(fields, 2)
    if ssid is None or key is None:
        raise ValueError(f"WifiInfoResponse missing ssid and/or key (got fields: {list(fields)})")
    return WifiInfo(
        ssid=ssid,
        key=key,
        bssid=protocol.get_string(fields, 3),
        security_mode_raw=protocol.get_varint(fields, 4),
        access_point_type_raw=protocol.get_varint(fields, 5),
    )
