"""encoder.py -- one raw RGB24 frame in, one H.264 access unit's worth of
NAL bytes out.

Two encoders, same output shape:
- PersistentEncoder (the daemon's default): one long-running ffmpeg,
  ~10ms per image on a Pi 4. See the "persistent encoder" section below.
- encode_frame_to_access_units: a fresh ffmpeg per image, ~330ms on a
  Pi 4 (mostly process startup), which caps the rate at ~3fps. Simplest
  possible, no state to corrupt; proven over 80,000+ images. Kept as the
  fallback (`serve --encoder per-image`).

aa_pi2display's live_demo.py also used a persistent ffmpeg and hit an
unresolved black-screen bug; the causes found there (frame pacing, a
silently dying reader thread, several messages per image) don't apply
here: frames are encoded on demand, one message per image, and any
encoder failure surfaces as an EncoderError. Every image is a keyframe
either way.

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

import queue
import shutil
import subprocess
import threading

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


# --- persistent encoder ---
#
# One long-running ffmpeg instead of one per image: ~10ms per image on a
# Pi 4 instead of ~330ms (measured 2026-09-25), which lifts the ~3fps cap.
# The catch with a long-running encoder is finding where one image's
# output ends: raw H.264 has no length fields, so the end of an image is
# only visible once the next one starts. So ffmpeg wraps its output in
# FLV instead, where every encoded image arrives as one tag with an exact
# byte length. SPS/PPS come once, in the FLV AVC sequence header, and
# are re-attached to every image so each message stays self-contained,
# exactly like the per-image encoder's output.
#
# Settings chosen so the stream matches the per-image encoder's as
# closely as possible: identical SPS apart from the declared frame rate,
# and the same fixed QP 20 on every image. `-r 25` rather than `-r 1`:
# with `-r 1` ffmpeg waits for several seconds' worth of input before
# encoding anything, and 25 keeps both 800x480 and 854x480 within level
# 3.0. One real difference remains: a continuous encoder alternates
# idr_pic_id 0/1 between images (per-image encodes are always 0).

PERSISTENT_FFMPEG_ARGS_TEMPLATE = [
    "ffmpeg", "-loglevel", "error", "-hide_banner",
    "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{width}x{height}", "-r", "25", "-i", "pipe:0",
    "-threads", "1",  # one slice per image -- see module docstring
    "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0", "-pix_fmt", "yuv420p",
    "-qp", "20", "-preset", "ultrafast", "-tune", "zerolatency",
    "-g", "1", "-bf", "0",
    "-f", "flv", "-flush_packets", "1", "pipe:1",
]

FLV_TAG_VIDEO = 9
AVC_SEQUENCE_HEADER, AVC_NALU = 0, 1


class FlvAvcReader:
    """Reads an FLV stream carrying H.264 and returns one image at a time
    as a list of NAL units (no start codes), SPS/PPS prepended."""

    def __init__(self, read_exact):
        self._read_exact = read_exact  # read_exact(n) -> exactly n bytes, or raises EOFError
        self._header_done = False
        self.sps: list[bytes] = []
        self.pps: list[bytes] = []

    def next_image(self) -> list[bytes]:
        if not self._header_done:
            header = self._read_exact(9)
            if header[:3] != b"FLV":
                raise EncoderError(f"not an FLV stream from ffmpeg (starts with {header[:3]!r})")
            self._read_exact(int.from_bytes(header[5:9], "big") - 9 + 4)  # rest of header + PreviousTagSize0
            self._header_done = True
        while True:
            tag = self._read_exact(11)
            data = self._read_exact(int.from_bytes(tag[1:4], "big"))
            self._read_exact(4)  # PreviousTagSize
            if tag[0] != FLV_TAG_VIDEO or len(data) < 5:
                continue  # script data (onMetaData) etc.
            packet_type, payload = data[1], data[5:]
            if packet_type == AVC_SEQUENCE_HEADER:
                self.sps, self.pps = parse_avcc(payload)
            elif packet_type == AVC_NALU:
                nals = split_length_prefixed(payload)
                return self.sps + self.pps + [n for n in nals if n and (n[0] & 0x1F) not in (7, 8)]


def parse_avcc(avcc: bytes) -> tuple[list[bytes], list[bytes]]:
    """AVCDecoderConfigurationRecord -> (SPS list, PPS list). Layout:
    5 fixed bytes, then numSPS (low 5 bits) + [u16 length, SPS]...,
    then numPPS + [u16 length, PPS]..."""
    def read_sets(i: int, count: int) -> tuple[list[bytes], int]:
        sets = []
        for _ in range(count):
            n = int.from_bytes(avcc[i:i + 2], "big")
            sets.append(avcc[i + 2:i + 2 + n])
            i += 2 + n
        return sets, i

    sps, i = read_sets(6, avcc[5] & 0x1F)
    pps, _ = read_sets(i + 1, avcc[i])
    return sps, pps


class PersistentEncoder:
    """Keeps one ffmpeg running. encode() takes one RGB24 image and returns
    its access units, same shape as encode_frame_to_access_units(). Not
    thread-safe: call from one thread at a time (the daemon's IPC thread).
    If ffmpeg dies or stalls, encode() raises EncoderError and the next
    call starts a fresh ffmpeg."""

    def __init__(self, *, timeout: float = 5.0):
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._geometry: tuple[int, int] | None = None
        self._results: queue.Queue = queue.Queue()
        self._stderr_tail: list[str] = []

    def encode(self, rgb24: bytes, width: int, height: int) -> list[list[bytes]]:
        expected = width * height * 3
        if len(rgb24) != expected:
            raise EncoderError(f"expected {expected} bytes of RGB24 data for {width}x{height}, got {len(rgb24)}")
        if self._proc is None or self._proc.poll() is not None or self._geometry != (width, height):
            self._start(width, height)
        try:
            self._proc.stdin.write(rgb24)
            self._proc.stdin.flush()
        except OSError as e:
            self.close()
            raise EncoderError(f"persistent ffmpeg stopped accepting input: {e}{self._stderr_hint()}") from e
        try:
            result = self._results.get(timeout=self.timeout)
        except queue.Empty:
            self.close()
            raise EncoderError(f"persistent ffmpeg produced nothing within {self.timeout}s{self._stderr_hint()}") from None
        if isinstance(result, Exception):
            self.close()
            raise EncoderError(f"persistent ffmpeg output ended: {result}{self._stderr_hint()}")
        return group_access_units(result)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _start(self, width: int, height: int) -> None:
        self.close()
        if shutil.which("ffmpeg") is None:
            raise EncoderError("ffmpeg not found -- install it (e.g. `sudo apt install ffmpeg`)")
        cmd = [arg.format(width=width, height=height) for arg in PERSISTENT_FFMPEG_ARGS_TEMPLATE]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._proc = proc
        self._geometry = (width, height)
        self._results = queue.Queue()  # never hand out a result left over from a previous process
        self._stderr_tail = []
        threading.Thread(target=self._read_output, args=(proc, self._results), daemon=True,
                         name="ffmpeg-output").start()
        threading.Thread(target=self._read_stderr, args=(proc, self._stderr_tail), daemon=True,
                         name="ffmpeg-stderr").start()

    @staticmethod
    def _read_output(proc: subprocess.Popen, results: queue.Queue) -> None:
        def read_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = proc.stdout.read(n - len(buf))
                if not chunk:
                    raise EOFError("ffmpeg closed its output")
                buf += chunk
            return buf

        reader = FlvAvcReader(read_exact)
        try:
            while True:
                results.put(reader.next_image())
        except Exception as e:  # noqa: BLE001 -- handed to encode() as the error
            results.put(e)

    @staticmethod
    def _read_stderr(proc: subprocess.Popen, tail: list[str]) -> None:
        for line in proc.stderr:
            tail.append(line.decode(errors="replace").rstrip())
            del tail[:-5]

    def _stderr_hint(self) -> str:
        return f" (ffmpeg said: {' | '.join(self._stderr_tail)})" if self._stderr_tail else ""


def split_length_prefixed(payload: bytes) -> list[bytes]:
    """4-byte-length-prefixed NAL units (FLV/MP4 style) -> list of NALs."""
    nals, i = [], 0
    while i + 4 <= len(payload):
        n = int.from_bytes(payload[i:i + 4], "big")
        nals.append(payload[i + 4:i + 4 + n])
        i += 4 + n
    return nals


def assemble_nal_bytes(access_unit: list[bytes]) -> bytes:
    """Re-adds the 4-byte Annex-B start code stripped by parse_nals --
    the wire format video_session.send_frame expects."""
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in access_unit)
