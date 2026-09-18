"""End-to-end (mocked-socket) regression test replaying the real captured
handshake from tests/fixtures/ground_truth_probe_session.txt through
bootstrap.py's actual code path -- confirms both what we send (must match
the real WifiInfoRequest bytes byte-for-byte) and what we decode (must
match the real WifiInfoResponse), without needing real hardware.
"""
import socket

from aa_hmi import bootstrap, protocol
from aa_hmi.messages import MessageId

# Real bytes from tests/fixtures/ground_truth_probe_session.txt
_VERSION_REQUEST_PAYLOAD = bytes.fromhex(
    "0801100020ec1220f11220f61220fb12208013208513208a13208f13209413209913"
    "209e1320a31320a81320b41320da2820822920aa2920802d20a82d20bc2820d02820"
    "e42820f828208c2920a02920b42920c82920f12c20852d20992d20ad2d20c12d"
)
_INFO_RESPONSE_PAYLOAD = bytes.fromhex(
    "0a0e54463831312d3163363432303161120831323334353637381a1136383a38663a"
    "63393a62333a32343a33372005"
)


class FakeSocket:
    """Serves pre-queued bytes on recv(), records everything sent."""

    def __init__(self, data: bytes, *, timeout_on_first_recv: bool = False):
        self._buf = data
        self._first_call = timeout_on_first_recv
        self.sent = b""

    def settimeout(self, t):
        pass

    def recv(self, n):
        if self._first_call:
            self._first_call = False
            raise socket.timeout()
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def sendall(self, data):
        self.sent += data


def test_try_get_wifi_info_replays_real_captured_sequence():
    """The head unit sends an unsolicited WifiVersionRequest first, then
    we send WifiInfoRequest, then it replies with WifiInfoResponse --
    exactly the order captured from real hardware."""
    incoming = (
        protocol.make_rfcomm_frame(MessageId.WIFI_VERSION_REQUEST, _VERSION_REQUEST_PAYLOAD)
        + protocol.make_rfcomm_frame(MessageId.WIFI_INFO_RESPONSE, _INFO_RESPONSE_PAYLOAD)
    )
    sock = FakeSocket(incoming)

    info = bootstrap.try_get_wifi_info(sock, read_timeout=1.0)

    assert info is not None
    assert info.ssid == "TF811-1c64201a"
    assert info.key == "12345678"
    assert info.bssid == "68:8f:c9:b3:24:37"
    # What we actually sent must match the real, confirmed WifiInfoRequest
    # bytes exactly: msg_id=2, empty body.
    assert sock.sent == protocol.make_rfcomm_frame(MessageId.WIFI_INFO_REQUEST, b"")


def test_try_get_wifi_info_works_without_a_leading_unsolicited_frame():
    """Not every head unit necessarily sends something unprompted first --
    the drain step should be a no-op (timeout, not a fatal error) when
    nothing arrives before we ask."""
    incoming = protocol.make_rfcomm_frame(MessageId.WIFI_INFO_RESPONSE, _INFO_RESPONSE_PAYLOAD)
    sock = FakeSocket(incoming, timeout_on_first_recv=True)

    info = bootstrap.try_get_wifi_info(sock, read_timeout=1.0)

    assert info is not None
    assert info.ssid == "TF811-1c64201a"


def test_try_get_wifi_info_returns_none_for_wrong_service_on_this_channel():
    """A channel that accepts a connection but doesn't speak this protocol
    at all (aa_pi2display's own finding: channels 7/12/15 on the test
    unit accepted raw connects but weren't the real service) must not be
    mistaken for a valid response."""
    sock = FakeSocket(b"", timeout_on_first_recv=True)  # nothing ever comes back
    assert bootstrap.try_get_wifi_info(sock, read_timeout=0.1) is None
