"""messages.py -- AA-Wireless Bluetooth bootstrap message IDs, and the
typed WifiInfo result decoded from a real WifiInfoResponse.

Message IDs are confirmed from aa-proxy-rs's public source
(https://github.com/aa-proxy/aa-proxy-rs, src/bluetooth.rs). The
WifiInfoResponse proto schema is confirmed from that same project's
src/protos/WifiInfoResponse.proto. See docs/protocol-notes.md for the full
writeup and the aa_pi2display sibling project's extracting-wifi-credentials.md
for the original decoded capture this was cross-checked against.

WifiInfoRequest's own outbound bytes (what must be *sent* to elicit a
WifiInfoResponse) are NOT yet confirmed -- see docs/capturing-ground-truth.md
for the capture procedure. Until that capture has been done and
REQUEST_BYTES below filled in for real, bootstrap.py refuses to guess bytes
at a real device.
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


# --- ground truth, filled in by Task 0 (docs/capturing-ground-truth.md) ---
#
# TODO(ground-truth): these are placeholders, NOT verified against a real
# capture yet. Each is either the literal payload bytes to send for that
# message, or None if the message is believed unnecessary/not yet confirmed
# needed for this tool's narrow scope (reaching WifiInfoResponse only).
#
# WifiInfoRequest has no known .proto file, which suggests (but does not
# confirm) an empty body -- a zero-length payload is a *plausible* guess,
# consistent with how this project's own TCP-side protocol has empty-body
# messages (e.g. AVChannelStopIndication in aa_session.py), but it is
# exactly that: a guess. bootstrap.py treats this as unconfirmed and will
# refuse to run against a real device until GROUND_TRUTH_CONFIRMED is
# flipped to True by whoever completes the capture in
# docs/capturing-ground-truth.md.
GROUND_TRUTH_CONFIRMED = False

WIFI_INFO_REQUEST_PAYLOAD: bytes | None = b""  # unconfirmed guess -- see above
WIFI_VERSION_REQUEST_PAYLOAD: bytes | None = None  # unconfirmed whether even needed first


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
