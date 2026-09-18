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


class VideoSessionError(AaHmiError):
    """The TCP/TLS video session (video_session.py) failed at some stage
    -- connect, handshake, channel open, or mid-session send/receive."""


class TlsHandshakeError(VideoSessionError):
    """The TLS 1.2 handshake with the display failed or never completed."""


class EncoderError(AaHmiError):
    """ffmpeg failed to encode a frame (encoder.py), or wasn't found."""


class IpcProtocolError(AaHmiError):
    """Malformed or out-of-sequence message on the local aa-hmi client
    <-> daemon socket (ipc/protocol.py, ipc/server.py, ipc/client.py)."""


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
# user's read-only nmcli calls (device/list) work fine. A one-time polkit
# rule fixes this permanently -- see docs/networkmanager-permissions.md
# (also confirmed live: `sudo aa-hmi` alone isn't a full fix either, if
# installed via pipx -- `~/.local/bin` isn't on sudo's secure_path).
HINT_NMCLI_NOT_AUTHORIZED = (
    "nmcli refused to create/activate this WiFi connection ('Not "
    "authorized to control networking'). This is a polkit permissions "
    "issue, not a bug -- a plain SSH session (no active console/logind "
    "session) doesn't get NetworkManager's default allow-without-auth "
    "policy on some distros. Permanent fix (one-time, ~30s): see "
    "docs/networkmanager-permissions.md. Quick alternative: "
    "'sudo $(command -v aa-hmi) run' (plain 'sudo aa-hmi' won't find the "
    "command if you installed via pipx, since ~/.local/bin isn't on "
    "sudo's PATH)."
)

HINT_VIDEO_SESSION_FLAKY = (
    "the TCP/TLS video session to the display keeps dropping. This can "
    "happen if the display power-cycled, moved out of WiFi range, or "
    "something else grabbed its single Bluetooth connection slot (see "
    "HINT_SINGLE_CONNECTION_SLOT above -- the daemon re-triggers the full "
    "Bluetooth bootstrap on every reconnect, which needs the same single "
    "BT connection slot as the initial one). If this keeps recurring, "
    "check docs/video-protocol-notes.md's session-persistence notes -- "
    "whether one Bluetooth trigger can hold a session open indefinitely "
    "is still an open question, being tracked via --persistent-session."
)

# errno values seen in practice against flaky classic-BT head units
# (aa_pi2display's extracting-wifi-credentials.md): EHOSTDOWN while a
# competing connection holds the radio, ECONNABORTED mid-negotiation,
# EALREADY from a connection attempt racing a previous one that hadn't
# fully torn down yet. All three are treated as retryable, not fatal.
RETRYABLE_ERRNOS = {112, 103, 114}  # EHOSTDOWN, ECONNABORTED, EALREADY
