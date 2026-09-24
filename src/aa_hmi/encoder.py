"""encoder.py -- one raw RGB24 frame in, one H.264 access unit's worth of
NAL bytes out.

Deliberately NOT a persistent ffmpeg pipe/thread architecture like
aa_pi2display's live_demo.py -- that's where an unresolved black-screen
bug lived (frame-pacing races, a silently-dying reader thread). At this
project's actual target rate (1 frame per 1-10s, never continuous video),
a fresh ffmpeg process per frame is simpler, has no persistent state to
corrupt, and costs on the order of 100ms on a Pi 4 -- irrelevant against
a multi-second budget. Every invocation encodes exactly one input image,
so it's always a keyframe by construction -- nothing to amortize across
frames, and nothing resembling the old sparse-keyframe/frame-pacing bug
categories can occur here at all.

parse_nals/group_access_units are ported verbatim from aa_session.py --
proven H.264 access-unit boundary logic (closes an AU at every VCL slice
NAL, type 1 or 5). This is deliberately NOT spec-correct AUD-based
grouping -- that was tried once, live, and made the black-screen problem
worse. Keep the non-spec-correct version; it's what's proven to work.
"""
from __future__ import annotations

import shutil
import subprocess

from .errors import EncoderError

FFMPEG_ARGS_TEMPLATE = [
    "ffmpeg", "-loglevel", "warning", "-y",
    "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{width}x{height}", "-r", "1", "-i", "pipe:0",
    "-frames:v", "{frames}",
    "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0", "-pix_fmt", "yuv420p",
    "-preset", "ultrafast", "-tune", "zerolatency",
    "-g", "1",  # kept defensively for clarity of intent -- likely a no-op given -frames:v 1 always emits one keyframe
    "-bf", "0",
    "-x264-params", "annexb=1",
    "-f", "h264", "pipe:1",
]


def encode_frame_to_h264(rgb24: bytes, width: int, height: int, *, timeout: float = 5.0,
                          copies: int = 1) -> bytes:
    """rgb24 must be exactly width*height*3 bytes (no padding/stride).
    Returns raw Annex-B H.264 bytes (a single keyframe access unit, or
    occasionally a keyframe plus a leading SPS/PPS/SEI -- callers should
    still run this through parse_nals/group_access_units rather than
    assuming a single NAL).

    copies > 1 feeds the same image that many times in one encoder run;
    see encode_frame_to_access_units(idr_pic_id_parity=1) for why."""
    expected = width * height * 3
    if len(rgb24) != expected:
        raise EncoderError(f"expected {expected} bytes of RGB24 data for {width}x{height}, got {len(rgb24)}")
    if shutil.which("ffmpeg") is None:
        raise EncoderError("ffmpeg not found -- install it (e.g. `sudo apt install ffmpeg`)")
    cmd = [arg.format(width=width, height=height, frames=copies) for arg in FFMPEG_ARGS_TEMPLATE]
    try:
        result = subprocess.run(cmd, input=rgb24 * copies, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise EncoderError(f"ffmpeg did not finish encoding within {timeout}s") from e
    if result.returncode != 0:
        raise EncoderError(f"ffmpeg exited {result.returncode}: {result.stderr.decode(errors='replace').strip()}")
    if not result.stdout:
        raise EncoderError("ffmpeg produced no output")
    return result.stdout


def parse_nals(data: bytes) -> list[bytes]:
    nals, starts, i = [], [], 0
    while True:
        idx = data.find(b"\x00\x00\x01", i)
        if idx == -1:
            break
        starts.append(idx)
        i = idx + 3
    for k, s in enumerate(starts):
        nal_start = s + 3
        nal_end = starts[k + 1] if k + 1 < len(starts) else len(data)
        nals.append(data[nal_start:nal_end])
    return nals


def group_access_units(nals: list[bytes]) -> list[list[bytes]]:
    """Group raw NALs into access units, one per video frame (closes an AU
    at every VCL slice NAL, types 1/5 -- bundling any preceding SPS/PPS/SEI
    with the slice they belong to)."""
    aus, current = [], []
    for nal in nals:
        if not nal:
            continue  # defensive: skip empty NALs (two start codes back-to-back) -- a real crash cause upstream once
        current.append(nal)
        if (nal[0] & 0x1F) in (1, 5):
            aus.append(current)
            current = []
    if current:
        aus.append(current)
    return aus


NAL_SPS, NAL_PPS = 7, 8


def _starts_picture(nal: bytes) -> bool:
    """True for a VCL slice NAL whose first_mb_in_slice is 0, i.e. the
    first slice of a new picture. first_mb_in_slice is the first ue(v) of
    the slice header; ue(0) is a single '1' bit, so it's the top bit of
    the byte right after the NAL header."""
    return len(nal) > 1 and (nal[0] & 0x1F) in (1, 5) and bool(nal[1] & 0x80)


def keep_last_picture(nals: list[bytes]) -> list[bytes]:
    """From a multi-picture encode, keep SPS/PPS (first occurrence of
    each) plus every NAL of the LAST picture, dropping earlier pictures."""
    starts = [i for i, n in enumerate(nals) if _starts_picture(n)]
    if not starts:
        return nals
    params, seen = [], set()
    for n in nals:
        t = n[0] & 0x1F if n else None
        if t in (NAL_SPS, NAL_PPS) and t not in seen:
            params.append(n)
            seen.add(t)
    tail = [n for n in nals[starts[-1]:] if n and (n[0] & 0x1F) not in (NAL_SPS, NAL_PPS)]
    return params + tail


def encode_frame_to_access_units(rgb24: bytes, width: int, height: int, *, timeout: float = 5.0,
                                  idr_pic_id_parity: int = 0) -> list[list[bytes]]:
    """Convenience wrapper: encode + parse + group in one call. Usually
    returns exactly one access unit (this whole module only ever encodes
    one input frame at a time), but callers should iterate the result
    rather than assume that, matching how aa_session.py always did.

    idr_pic_id_parity: because every image is a fresh encoder run, every
    image came out as an IDR picture with idr_pic_id=0 (confirmed with
    ffmpeg's trace_headers, 2026-09-24). The H.264 spec requires two
    consecutive IDR pictures to have DIFFERENT idr_pic_id values -- it's
    one of the ways a decoder tells a new picture apart from more slices
    of the previous one -- so our stream violated that on every image.
    Leading suspect for the display freezing after a variable number of
    images. With parity=1, the same image is encoded twice in one run and
    only the second copy is kept; the encoder itself then gives it
    idr_pic_id=1. Alternating 0/1 per image makes the stream conform
    without rewriting any bits by hand."""
    if idr_pic_id_parity:
        h264 = encode_frame_to_h264(rgb24, width, height, timeout=timeout, copies=2)
        nals = keep_last_picture(parse_nals(h264))
    else:
        nals = parse_nals(encode_frame_to_h264(rgb24, width, height, timeout=timeout))
    return group_access_units(nals)


def assemble_nal_bytes(access_unit: list[bytes]) -> bytes:
    """Re-adds the 4-byte Annex-B start code stripped by parse_nals --
    the wire format video_session.send_frame expects."""
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in access_unit)
