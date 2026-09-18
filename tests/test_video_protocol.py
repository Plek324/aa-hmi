import struct

import pytest

from aa_hmi import video_protocol as wire


def test_make_wire_frame_header_layout():
    payload = b"hello"
    frame = wire.make_wire_frame(channel=3, flags=0x0B, body=payload)
    assert frame[0] == 3
    assert frame[1] == 0x0B
    assert frame[2:4] == len(payload).to_bytes(2, "big")
    assert frame[4:] == payload


class FakeSocket:
    def __init__(self, data: bytes):
        self._buf = data

    def settimeout(self, t):
        pass

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


def test_read_wire_frame_roundtrip():
    frame = wire.make_wire_frame(3, 0x0B, b"payload bytes")
    sock = FakeSocket(frame)
    channel, flags, body = wire.read_wire_frame(sock)
    assert (channel, flags, body) == (3, 0x0B, b"payload bytes")


def test_read_wire_frame_returns_none_on_clean_eof():
    assert wire.read_wire_frame(FakeSocket(b"")) is None


def test_read_wire_frame_raises_on_partial_body():
    header = struct.pack(">BBH", 3, 0x0B, 10)  # says 10 bytes body
    sock = FakeSocket(header + b"short")  # only 5 bytes then EOF
    with pytest.raises(wire.WireFrameError):
        wire.read_wire_frame(sock)


def test_split_tls_records_single_record():
    record = b"\x17\x03\x03" + struct.pack(">H", 5) + b"hello"
    assert wire.split_tls_records(record) == [record]


def test_split_tls_records_two_records_concatenated():
    """The one proven bug fix in this file: a >16KB write can produce
    ciphertext spanning multiple real TLS records, and the display can't
    handle them bundled under one wire frame -- confirmed to cause a
    black screen. This must always split cleanly."""
    r1 = b"\x17\x03\x03" + struct.pack(">H", 4) + b"AAAA"
    r2 = b"\x17\x03\x03" + struct.pack(">H", 6) + b"BBBBBB"
    combined = r1 + r2
    records = wire.split_tls_records(combined)
    assert records == [r1, r2]
    assert len(records) == 2  # not 1 -- this is the exact regression this fix guards against


def test_split_tls_records_handles_a_realistic_max_size_record():
    # RFC 5246: max 16384 bytes of plaintext per TLS record.
    payload = b"x" * 16384
    r1 = b"\x17\x03\x03" + struct.pack(">H", len(payload)) + payload
    r2 = b"\x17\x03\x03" + struct.pack(">H", 3) + b"abc"
    records = wire.split_tls_records(r1 + r2)
    assert len(records) == 2
    assert records[0] == r1
    assert records[1] == r2


def test_split_tls_records_empty_input():
    assert wire.split_tls_records(b"") == []
