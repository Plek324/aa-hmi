import shutil

import pytest

from aa_hmi import encoder
from aa_hmi.errors import EncoderError

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_parse_nals_and_group_access_units_fixture():
    """Fixture-based test of the ported (proven) AU-boundary logic --
    aa_pi2display never had this test, but the logic deserves one:
    closes an access unit at every VCL slice NAL (type 1 or 5), NOT at
    spec-correct AUD boundaries -- that's deliberate, see encoder.py."""
    # SPS (type 7), PPS (type 8), then an IDR slice (type 5) -- one AU.
    # Then a plain slice (type 1) -- a second AU on its own.
    data = (
        b"\x00\x00\x01\x67\xAA\xBB"          # SPS
        b"\x00\x00\x01\x68\xCC"              # PPS
        b"\x00\x00\x01\x65\xDD\xEE\xFF"       # IDR slice (type 5)
        b"\x00\x00\x01\x41\x11\x22"          # plain slice (type 1)
    )
    nals = encoder.parse_nals(data)
    assert len(nals) == 4
    assert nals[0][0] & 0x1F == 7
    assert nals[2][0] & 0x1F == 5

    aus = encoder.group_access_units(nals)
    assert len(aus) == 2
    assert len(aus[0]) == 3  # SPS + PPS + IDR slice bundled into the first AU
    assert len(aus[1]) == 1  # the plain slice is its own AU


def test_group_access_units_skips_empty_nals():
    """Defensive fix ported from the sibling project -- indexing nal[0]
    on an empty NAL (two start codes back-to-back) was a real crash
    cause once (silently killed a reader thread there); must not
    reproduce here."""
    data = b"\x00\x00\x01\x00\x00\x01\x65\xAA"  # empty NAL, then a real IDR slice
    nals = encoder.parse_nals(data)
    aus = encoder.group_access_units(nals)
    assert len(aus) == 1
    assert aus[0] == [b"\x65\xAA"]


def test_assemble_nal_bytes_readds_annexb_start_codes():
    result = encoder.assemble_nal_bytes([b"\x67\xAA", b"\x65\xBB"])
    assert result == b"\x00\x00\x00\x01\x67\xAA\x00\x00\x00\x01\x65\xBB"


def test_encode_frame_to_h264_rejects_wrong_sized_input():
    with pytest.raises(EncoderError, match="expected"):
        encoder.encode_frame_to_h264(b"too short", width=854, height=480)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_encode_frame_to_h264_real_ffmpeg_produces_annexb_keyframe():
    width, height = 16, 16  # tiny, for a fast test
    rgb24 = bytes([128, 64, 200] * (width * height))
    h264 = encoder.encode_frame_to_h264(rgb24, width, height, timeout=10.0)
    assert h264.startswith(b"\x00\x00\x00\x01") or h264.startswith(b"\x00\x00\x01")

    aus = encoder.group_access_units(encoder.parse_nals(h264))
    assert len(aus) >= 1
    # at least one NAL across all AUs must be an IDR slice (type 5) --
    # every single-frame encode is a keyframe by construction.
    all_nals = [nal for au in aus for nal in au]
    assert any((nal[0] & 0x1F) == 5 for nal in all_nals if nal)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_encode_frame_to_access_units_convenience_wrapper():
    width, height = 16, 16
    rgb24 = bytes([10, 20, 30] * (width * height))
    aus = encoder.encode_frame_to_access_units(rgb24, width, height, timeout=10.0)
    assert len(aus) >= 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_encode_frame_to_access_units_gives_one_slice_per_image():
    """The display freeze fix: one slice = one media message per image."""
    width, height = 320, 240  # big enough that sliced threads would split it
    rgb24 = bytes(range(256)) * (width * height * 3 // 256)
    aus = encoder.encode_frame_to_access_units(rgb24, width, height, timeout=10.0)
    slices = [n for au in aus for n in au if (n[0] & 0x1F) in (1, 5)]
    assert len(slices) == 1


# --- persistent encoder: FLV parsing ---

SPS, PPS, SEI, IDR = b"\x67\x42\xc0\x1e", b"\x68\xce\x3c\x80", b"\x06\x05\x01", b"\x65\x88\x84"


def _flv_tag(tag_type, data):
    return (bytes([tag_type]) + len(data).to_bytes(3, "big") + b"\x00" * 7 + data
            + (11 + len(data)).to_bytes(4, "big"))


def _avcc(sps, pps):
    return (b"\x01\x42\xc0\x1e\xff" + bytes([0xE0 | 1]) + len(sps).to_bytes(2, "big") + sps
            + b"\x01" + len(pps).to_bytes(2, "big") + pps)


def _flv_stream(*images):
    out = b"FLV\x01\x01\x00\x00\x00\x09" + b"\x00" * 4
    out += _flv_tag(18, b"onMetaData...")  # script tag, must be skipped
    out += _flv_tag(9, b"\x17\x00\x00\x00\x00" + _avcc(SPS, PPS))
    for nals in images:
        body = b"".join(len(n).to_bytes(4, "big") + n for n in nals)
        out += _flv_tag(9, b"\x17\x01\x00\x00\x00" + body)
    return out


def _reader_for(data):
    pos = [0]

    def read_exact(n):
        if pos[0] + n > len(data):
            raise EOFError
        chunk = data[pos[0]:pos[0] + n]
        pos[0] += n
        return chunk
    return encoder.FlvAvcReader(read_exact)


def test_parse_avcc():
    assert encoder.parse_avcc(_avcc(SPS, PPS)) == ([SPS], [PPS])


def test_split_length_prefixed():
    payload = (3).to_bytes(4, "big") + b"abc" + (2).to_bytes(4, "big") + b"de"
    assert encoder.split_length_prefixed(payload) == [b"abc", b"de"]


def test_flv_reader_returns_one_image_at_a_time_with_sps_pps_attached():
    reader = _reader_for(_flv_stream([SEI, IDR], [IDR]))
    assert reader.next_image() == [SPS, PPS, SEI, IDR]
    assert reader.next_image() == [SPS, PPS, IDR]
    with pytest.raises(EOFError):
        reader.next_image()


def test_flv_reader_drops_in_band_sps_pps_to_avoid_duplicates():
    reader = _reader_for(_flv_stream([SPS, PPS, IDR]))
    assert reader.next_image() == [SPS, PPS, IDR]


def test_flv_reader_rejects_non_flv():
    with pytest.raises(EncoderError):
        _reader_for(b"garbage!!" + b"\x00" * 20).next_image()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_persistent_encoder_real_ffmpeg():
    width, height = 320, 240
    enc = encoder.PersistentEncoder(timeout=10.0)
    try:
        for shade in (10, 200, 90):
            aus = enc.encode(bytes([shade, 100, 50]) * (width * height), width, height)
            assert len(aus) == 1  # one message per image
            types = [n[0] & 0x1F for n in aus[0]]
            assert types[:2] == [7, 8] and types[-1] == 5  # SPS, PPS, ..., IDR slice
    finally:
        enc.close()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_persistent_encoder_recovers_after_ffmpeg_dies():
    width, height = 64, 64
    enc = encoder.PersistentEncoder(timeout=10.0)
    try:
        enc.encode(b"\x00" * (width * height * 3), width, height)
        first_proc = enc._proc
        first_proc.kill()
        first_proc.wait()
        # A dead ffmpeg is noticed before the next image and replaced.
        assert len(enc.encode(b"\x00" * (width * height * 3), width, height)) == 1
        assert enc._proc is not first_proc
    finally:
        enc.close()
