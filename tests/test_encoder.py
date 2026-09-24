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


# --- keep_last_picture: idr_pic_id alternation (display-freeze fix) ---
# NAL header 0x67 = SPS, 0x68 = PPS, 0x06 = SEI, 0x65 = IDR slice. The
# byte after a slice's header has its top bit set iff first_mb_in_slice==0
# (start of a new picture).

def test_keep_last_picture_keeps_params_and_only_the_second_picture():
    nals = [
        b"\x67sps", b"\x68pps", b"\x06sei",
        b"\x65\x88pic1-a", b"\x65\x40pic1-b",
        b"\x65\x88pic2-a", b"\x65\x40pic2-b",
    ]
    assert encoder.keep_last_picture(nals) == [b"\x67sps", b"\x68pps", b"\x65\x88pic2-a", b"\x65\x40pic2-b"]


def test_keep_last_picture_single_picture_is_unchanged_apart_from_ordering():
    nals = [b"\x67sps", b"\x68pps", b"\x65\x88a", b"\x65\x40b"]
    assert encoder.keep_last_picture(nals) == nals


def test_keep_last_picture_no_slices_returns_input():
    nals = [b"\x67sps", b"\x68pps"]
    assert encoder.keep_last_picture(nals) == nals


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_parity_1_returns_exactly_one_picture():
    width, height = 16, 16
    rgb24 = bytes([10, 20, 30] * (width * height))
    aus = encoder.encode_frame_to_access_units(rgb24, width, height, timeout=10.0, idr_pic_id_parity=1)
    slices = [n for au in aus for n in au if (n[0] & 0x1F) in (1, 5)]
    assert sum(1 for n in slices if n[1] & 0x80) == 1
