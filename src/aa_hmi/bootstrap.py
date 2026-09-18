"""bootstrap.py -- orchestrate the RFCOMM WiFi-bootstrap handshake.

Deliberately narrow scope: this only implements what's needed to reach a
WifiInfoResponse. WifiStartRequest and the ping/keepalive messages belong
to the AA-video-session "arm the TCP port" step, which is a concern for a
consumer of this tool (like aa_pi2display) -- not this tool's own job,
which ends at "here are the WiFi credentials." See docs/protocol-notes.md.
"""
from __future__ import annotations

import socket

from . import messages, protocol
from .errors import GroundTruthNotConfirmedError, HandshakeError
from .log import log
from .messages import MessageId, WifiInfo


def _send_wifi_info_request(sock: socket.socket) -> None:
    if not messages.GROUND_TRUTH_CONFIRMED:
        raise GroundTruthNotConfirmedError(
            "WifiInfoRequest's exact bytes have not been confirmed against a "
            "real captured session yet (messages.GROUND_TRUTH_CONFIRMED is "
            "False). Refusing to guess bytes at a real device -- complete "
            "the capture procedure in docs/capturing-ground-truth.md first, "
            "then fill in messages.WIFI_INFO_REQUEST_PAYLOAD and flip "
            "GROUND_TRUTH_CONFIRMED to True."
        )
    payload = messages.WIFI_INFO_REQUEST_PAYLOAD or b""
    frame = protocol.make_rfcomm_frame(MessageId.WIFI_INFO_REQUEST, payload)
    sock.sendall(frame)


def _drain_unsolicited_frame(sock: socket.socket, timeout: float) -> None:
    """A real head unit sends one frame unprompted right after the RFCOMM
    connection opens, before we've asked for anything -- confirmed via a
    real capture: WifiVersionRequest (id=4), containing its supported WiFi
    channel list. We don't need to respond to it (the real aa-proxy-rs
    probe doesn't either), just not mistake it for the WifiInfoResponse
    we're about to ask for. Best-effort: if nothing arrives within
    `timeout`, that's fine too -- some head units may not send anything
    here, so this is a drain, not a required handshake step."""
    try:
        frame = protocol.read_rfcomm_frame(sock, timeout=timeout)
    except (socket.timeout, protocol.FrameError):
        return
    if frame is not None:
        msg_id, payload = frame
        log(f"  drained unsolicited frame msg_id={msg_id:#06x} len={len(payload)} "
            f"before requesting WifiInfo", verbose_only=True, verbose=True)


def try_get_wifi_info(sock: socket.socket, *, read_timeout: float = 2.0) -> WifiInfo | None:
    """Used as the `validate` callback for rfcomm.find_channel: drain any
    unsolicited leading frame, send WifiInfoRequest, read one frame back,
    and return a WifiInfo if (and only if) it decodes as a structurally
    valid WifiInfoResponse -- this is what actually distinguishes the real
    AA-Wireless service channel from other unrelated services that also
    happen to accept a raw RFCOMM connection (aa_pi2display found this the
    hard way: channels 7/12/15 all accepted connections on the test unit,
    only channel 4 was real). Returns None (not an exception) for "channel
    doesn't speak this protocol" -- exceptions are reserved for real
    transport errors.
    """
    _drain_unsolicited_frame(sock, timeout=read_timeout)
    _send_wifi_info_request(sock)
    try:
        frame = protocol.read_rfcomm_frame(sock, timeout=read_timeout)
    except (socket.timeout, protocol.FrameError):
        return None
    if frame is None:
        return None
    msg_id, payload = frame
    if msg_id != MessageId.WIFI_INFO_RESPONSE:
        return None
    try:
        return messages.parse_wifi_info_response(payload)
    except ValueError:
        return None


def confirm_wifi_connected(sock: socket.socket) -> None:
    """Send WifiStartResponse + WifiConnectStatus over an already-open
    RFCOMM socket, immediately after a successful WifiInfoResponse.

    Deliberately NOT called by `try_get_wifi_info`/`get_wifi_info`
    themselves, or by `aa-hmi run` -- this project's own credential-only
    scope never needed it (see this module's docstring). But `aa-hmi
    serve` (daemon.py) discovered live that skipping these two messages
    means the display's TCP video port (29880) refuses connections even
    right after successfully joining its WiFi network -- these appear to
    be what actually arms the video listener. Callers that need the video
    session (daemon.py) must call this before closing the RFCOMM socket;
    callers that only want credentials (cli.py's `run`) should not."""
    for msg_id, payload in (
        (MessageId.WIFI_START_RESPONSE, messages.WIFI_START_RESPONSE_PAYLOAD),
        (MessageId.WIFI_CONNECT_STATUS, messages.WIFI_CONNECT_STATUS_PAYLOAD),
    ):
        sock.sendall(protocol.make_rfcomm_frame(msg_id, payload))
    log("  sent WifiStartResponse + WifiConnectStatus (arms the display's TCP video listener)")


def get_wifi_info(sock: socket.socket, *, read_timeout: float = 5.0) -> WifiInfo:
    """Like try_get_wifi_info, but for the case where the channel is
    already known-good (cached) -- raises HandshakeError with a real
    message instead of silently returning None, since here a failure is
    unexpected rather than "still probing candidate channels."""
    info = try_get_wifi_info(sock, read_timeout=read_timeout)
    if info is None:
        raise HandshakeError(
            "no valid WifiInfoResponse received on the cached RFCOMM "
            "channel -- the head unit may be unpaired, out of range, or "
            "its BT radio may be held by another connection (see "
            "errors.HINT_SINGLE_CONNECTION_SLOT). Try --rescan."
        )
    log(f"  got WiFi info: ssid={info.ssid!r} bssid={info.bssid} "
        f"security_mode_raw={info.security_mode_raw}")
    return info
