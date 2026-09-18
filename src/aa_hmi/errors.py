"""errors.py -- typed exceptions, plus canned actionable-hint strings for
known hardware quirks documented in the aa_pi2display sibling project.

Keeping these hint strings as named constants (rather than inlined in
retry.py/bootstrap.py) makes it easy for contributors to refine the wording
per their own hardware's actual error signatures without hunting through
control-flow code.
"""


class AaHmiError(Exception):
    """Base class for this tool's own exceptions."""


class NoDeviceSelectedError(AaHmiError):
    """User cancelled the interactive picker, or no cached device and
    --non-interactive was given."""


class GroundTruthNotConfirmedError(AaHmiError):
    """bootstrap.py refuses to send unconfirmed guessed bytes at a real
    device -- see docs/capturing-ground-truth.md."""


class NoChannelFoundError(AaHmiError):
    """Every candidate RFCOMM channel (SDP-suggested + brute-force 1-30)
    either refused a connection or didn't speak the expected protocol."""


class HandshakeError(AaHmiError):
    """Connected on a channel, but the bootstrap handshake itself failed
    (bad/short response, WifiInfoResponse missing ssid/key, etc)."""


class NmcliError(AaHmiError):
    """A checked nmcli invocation returned a non-zero exit code."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(cmd)!r} exited {returncode}: {stderr.strip()}")


class BluetoothCtlError(AaHmiError):
    """A bluetoothctl invocation failed or timed out."""


# --- actionable hints for known-flaky conditions (real hardware quirks) ---

HINT_SINGLE_CONNECTION_SLOT = (
    "still failing after several attempts -- this head unit's classic "
    "Bluetooth radio can only hold ONE connection at a time. If a phone "
    "(or anything else) is currently connected/paired to it, that's very "
    "likely why: disconnect or forget the head unit on that other device "
    "first, then try again."
)

HINT_FLAKY_CLASSIC_BT = (
    "this unit's classic Bluetooth stack can be intermittently "
    "unresponsive for no obvious reason -- this has been observed to take "
    "several minutes of retries to clear on some hardware. If it's been "
    "failing for a long time, try power-cycling the head unit."
)

# Confirmed live (2026-09-18): nmcli's "Not authorized to control
# networking" is a polkit permissions issue, not a bug -- a plain SSH
# session (no active console/logind session) doesn't get NetworkManager's
# default allow-without-auth policy on some distros, even though the same
# user's read-only nmcli calls (device/list) work fine. `sudo nmcli ...`
# from the exact same session succeeded immediately.
HINT_NMCLI_NOT_AUTHORIZED = (
    "nmcli refused to create/activate this WiFi connection ('Not "
    "authorized to control networking'). This is a polkit permissions "
    "issue, not a bug -- some setups (e.g. a plain SSH session with no "
    "active console/logind session) don't get NetworkManager's default "
    "allow-without-auth policy, even though read-only nmcli commands "
    "still work fine. Fix: run aa-hmi with sudo, or grant your user the "
    "org.freedesktop.NetworkManager.network-control polkit action."
)

# errno values seen in practice against flaky classic-BT head units
# (aa_pi2display's extracting-wifi-credentials.md): EHOSTDOWN while a
# competing connection holds the radio, ECONNABORTED mid-negotiation,
# EALREADY from a connection attempt racing a previous one that hadn't
# fully torn down yet. All three are treated as retryable, not fatal.
RETRYABLE_ERRNOS = {112, 103, 114}  # EHOSTDOWN, ECONNABORTED, EALREADY
