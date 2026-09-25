"""encoder.py -- one raw RGB24 frame in, one H.264 access unit's worth of
NAL bytes out.

Deliberately NOT a persistent ffmpeg pipe/thread architecture like
aa_pi2display's live_demo.py -- that's where an unresolved black-screen
bug lived (frame-pacing races, a silently-dying reader thread). A fresh
ffmpeg process per frame is simpler and has no persistent state to
corrupt. It costs ~330ms on a Pi 4 (mostly process startup), which caps
the frame rate at ~3fps. Every invocation encodes exactly one input
image, so it's always a keyframe by construction.

One slice per image (`-threads 1`) is essential: with x264's default
sliced threads each image came out as one slice per CPU core, each sent
as its own media message, and the display's decoder froze after 1-145
images. A real phone sends one whole picture per message; so do we now
(verified: 80,701 images overnight without a freeze). See
docs/video-protocol-notes.md.

parse_nals/group_access_units are ported from aa_session.py: an access
unit closes at every VCL slice NAL (type 1 or 5), bundling any preceding
SPS/PPS/SEI with it. With one slice per image that's one access unit =
one message per image.
"""
from __future__ import annotations

import shutil
import subprocess

from .errors import EncoderError

FFMPEG_ARGS_TEMPLATE = [
    "ffmpeg", "-loglevel", "warning", "-y",
    "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{width}x{height}", "-r", "1", "-i", "pipe:0",
    "-frames:v", "1",
    "-threads", "1",  # one slice per image -- see module docstring
    "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0", "-pix_fmt", "yuv420p",
    "-preset", "ultrafast", "-tune", "zerolatency",
    "-g", "1",  # kept defensively for clarity of intent -- likely a no-op given -frames:v 1 always emits one keyframe
    "-bf", "0",
    "-x264-params", "annexb=1",
    "-f", "h264", "pipe:1",
]


def encode_frame_to_h264(rgb24: bytes, width: int, height: int, *, timeout: float = 5.0) -> bytes:
    """rgb24 must be exactly width*height*3 bytes (no padding/stride).
    Returns raw Annex-B H.264 bytes (a single keyframe access unit, or
    occasionally a keyframe plus a leading SPS/PPS/SEI -- callers should
    still run this through parse_nals/group_access_units rather than
    assuming a single NAL)."""
    expected = width * height * 3
    if len(rgb24) != expected:
        raise EncoderError(f"expected {expected} bytes of RGB24 data for {width}x{height}, got {len(rgb24)}")
    if shutil.which("ffmpeg") is None:
        raise EncoderError("ffmpeg not found -- install it (e.g. `sudo apt install ffmpeg`)")
    cmd = [arg.format(width=width, height=height) for arg in FFMPEG_ARGS_TEMPLATE]
    try:
        result = subprocess.run(cmd, input=rgb24, capture_output=True, timeout=timeout)
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


def encode_frame_to_access_units(rgb24: bytes, width: int, height: int, *, timeout: float = 5.0) -> list[list[bytes]]:
    """Convenience wrapper: encode + parse + group in one call. Returns one
    access unit per image in practice, but callers should iterate the
    result rather than assume that."""
    return group_access_units(parse_nals(encode_frame_to_h264(rgb24, width, height, timeout=timeout)))


def assemble_nal_bytes(access_unit: list[bytes]) -> bytes:
    """Re-adds the 4-byte Annex-B start code stripped by parse_nals --
    the wire format video_session.send_frame expects."""
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in access_unit)
