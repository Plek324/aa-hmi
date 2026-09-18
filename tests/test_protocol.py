from pathlib import Path

import pytest

from aa_hmi import protocol

FIXTURES = Path(__file__).parent / "fixtures"


def _load_hex_fixture(name: str) -> bytes:
    lines = (FIXTURES / name).read_text().splitlines()
    hex_lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    return bytes.fromhex("".join(hex_lines))


def test_varint_roundtrip():
    for n in [0, 1, 127, 128, 300, 16384, 2**32, 2**63]:
        encoded = protocol.encode_varint(n)
        decoded, i = protocol.read_varint(encoded, 0)
        assert decoded == n
        assert i == len(encoded)


def test_string_field_roundtrip():
    field = protocol.encode_string_field(1, "TF811-1c64201a")
    fields = protocol.decode_fields(field)
    assert protocol.get_string(fields, 1) == "TF811-1c64201a"


def test_varint_field_roundtrip():
    field = protocol.encode_varint_field(4, 5)
    fields = protocol.decode_fields(field)
    assert protocol.get_varint(fields, 4) == 5


def test_rfcomm_frame_header_roundtrip():
    payload = b"hello world"
    frame = protocol.make_rfcomm_frame(0x0002, payload)
    assert frame[:2] == len(payload).to_bytes(2, "big")
    assert frame[2:4] == (2).to_bytes(2, "big")
    assert frame[4:] == payload


def test_decode_real_wifi_info_response_fixture():
    """The single highest-value regression test in this repo: a real
    captured WifiInfoResponse from a TF811BT display must decode cleanly
    despite an out-of-spec security_mode value and a missing (but
    `required`-per-proto) access_point_type field."""
    payload = _load_hex_fixture("wifi_info_response_tf811.hex")
    fields = protocol.decode_fields(payload)

    assert protocol.get_string(fields, 1) == "TF811-1c64201a"  # ssid
    assert protocol.get_string(fields, 2) == "12345678"        # key
    assert protocol.get_string(fields, 3) == "68:8f:c9:b3:24:37"  # bssid
    assert protocol.get_varint(fields, 4) == 5   # security_mode -- NOT a valid enum value
    assert protocol.get_varint(fields, 5) is None  # access_point_type -- absent


def test_decode_fields_does_not_raise_on_unknown_wire_type():
    # tag byte encodes field_num=1, wire_type=6 (unknown/reserved)
    garbage = bytes([0x0E]) + b"trailing garbage that should just be ignored"
    fields = protocol.decode_fields(garbage)
    assert fields == {}


def test_decode_fields_does_not_raise_on_truncated_length_delimited():
    # field 1, wire_type 2, length byte says 50 bytes but only 3 follow
    truncated = bytes([0x0A, 50]) + b"abc"
    fields = protocol.decode_fields(truncated)
    assert fields == {}  # stops cleanly, no exception


def test_read_rfcomm_frame_returns_none_on_clean_eof():
    import socket

    class FakeSocket:
        def settimeout(self, t):
            pass

        def recv(self, n):
            return b""

    assert protocol.read_rfcomm_frame(FakeSocket()) is None


def test_read_rfcomm_frame_raises_on_partial_payload():
    class FakeSocket:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def settimeout(self, t):
            pass

        def recv(self, n):
            return self._chunks.pop(0) if self._chunks else b""

    header = (5).to_bytes(2, "big") + (3).to_bytes(2, "big")  # says 5 bytes payload
    sock = FakeSocket([header, b"ab"])  # only 2 bytes then EOF
    with pytest.raises(protocol.FrameError):
        protocol.read_rfcomm_frame(sock)
