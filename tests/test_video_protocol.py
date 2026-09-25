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


# --- multi-frame messages (FIRST / MIDDLE / LAST) ---

def test_split_plaintext_small_message_is_one_chunk():
    assert wire.split_plaintext(b"x" * 16384) == [b"x" * 16384]


def test_split_plaintext_large_message_chunks_at_16384():
    chunks = wire.split_plaintext(b"x" * 40000)
    assert [len(c) for c in chunks] == [16384, 16384, 7232]


def test_fragment_flags_single_frame_is_bulk():
    assert wire.fragment_flags(0x0B, 0, 1) == 0x0B


def test_fragment_flags_first_middle_last():
    assert [wire.fragment_flags(0x0B, i, 3) for i in range(3)] == [0x09, 0x08, 0x0A]


def test_make_fragment_frame_first_carries_total_size():
    frame = wire.make_fragment_frame(3, 0x09, b"abc", 40000)
    assert frame == bytes([3, 0x09, 0, 3]) + (40000).to_bytes(4, "big") + b"abc"


def test_make_fragment_frame_bulk_and_last_have_plain_header():
    assert wire.make_fragment_frame(3, 0x0B, b"abc", 3) == bytes([3, 0x0B, 0, 3]) + b"abc"
    assert wire.make_fragment_frame(3, 0x0A, b"abc", 40000) == bytes([3, 0x0A, 0, 3]) + b"abc"
