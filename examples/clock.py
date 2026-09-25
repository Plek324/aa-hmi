#!/usr/bin/env python3
"""clock.py -- render the current time every --interval seconds and push
it to the display via aa-hmi. Doubles as a soak-test tool: leave it
running for hours against real hardware to gather data on the open
"does a video session survive without periodic Bluetooth re-arming"
question (see docs/video-protocol-notes.md) -- aa-hmi serve logs every
reconnect it does on its own side; this script just needs to keep
producing frames.

Start `aa-hmi serve` first, then run this. Same client-library-only
import shape as hello_world.py.
"""
import argparse
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "src")  # allow running straight from a repo checkout without installing
from aa_hmi.ipc.client import AaHmiClient  # noqa: E402

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _load_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_clock(width: int, height: int, font_big, font_small) -> bytes:
    img = Image.new("RGB", (width, height), (20, 30, 60))
    draw = ImageDraw.Draw(img)
    draw.text((width // 2, height // 2 - 30), time.strftime("%H:%M:%S"),
              font=font_big, fill=(255, 255, 255), anchor="mm")
    draw.text((width // 2, height // 2 + 60), time.strftime("%Y-%m-%d"),
              font=font_small, fill=(160, 180, 220), anchor="mm")
    return img.tobytes()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=float, default=5.0,
                    help="seconds between frames (default: 5 -- the target rate this project was designed around)")
    args = p.parse_args()

    with AaHmiClient() as client:
        print(f"connected -- daemon expects {client.frame_width}x{client.frame_height} "
              f"{client.pixel_format.name} frames, sending every {args.interval}s (Ctrl+C to quit)")
        font_big = _load_font(90)
        font_small = _load_font(32)
        sent = 0
        try:
            while True:
                frame = render_clock(client.frame_width, client.frame_height, font_big, font_small)
                client.send_frame(frame)
                sent += 1
                if sent % 12 == 0:  # roughly once a minute at the default 5s interval
                    print(f"[{time.strftime('%H:%M:%S')}] sent {sent} frames so far")
                deadline = time.monotonic() + args.interval
                while time.monotonic() < deadline:
                    touch = client.poll_touch(timeout=max(0.0, deadline - time.monotonic()))
                    if touch and touch.is_structured:
                        print(f"touch: {touch.action.name} at ({touch.x}, {touch.y})")
                    else:
                        break
        except KeyboardInterrupt:
            print(f"\nbye -- sent {sent} frames total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
