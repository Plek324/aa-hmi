#!/usr/bin/env python3
"""stress.py -- push deliberately hard-to-compress images to the display,
to test messages over 16KB (sent as several pieces, see
docs/video-protocol-notes.md).

Part of each image is random noise, which H.264 can barely compress: the
bigger the noise area, the bigger the encoded image. A label shows the
image number and noise level, and a bar moves along the bottom edge, so
you can see on the display whether every image arrives intact.

Start `aa-hmi serve -v` first: its log shows each image's size, and
"in N pieces" when an image needed more than one piece.

Examples:
    python3 examples/stress.py --noise 30         # fixed level
    python3 examples/stress.py --ramp             # step through 0..100%
"""
import argparse
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "src")  # allow running straight from a repo checkout without installing
from aa_hmi.ipc.client import AaHmiClient  # noqa: E402

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
RAMP_LEVELS = [0, 2, 5, 10, 20, 40, 70, 100]


def _load_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(width: int, height: int, number: int, noise_percent: int, font) -> bytes:
    img = Image.new("RGB", (width, height), (20, 30, 60))
    noise_width = width * noise_percent // 100
    if noise_width:
        img.paste(Image.frombytes("RGB", (noise_width, height), os.urandom(noise_width * height * 3)), (0, 0))
    draw = ImageDraw.Draw(img)
    label = f"#{number}  noise {noise_percent}%"
    box = draw.textbbox((width // 2, height // 2), label, font=font, anchor="mm")
    draw.rectangle((box[0] - 20, box[1] - 15, box[2] + 20, box[3] + 15), fill=(0, 0, 0))
    draw.text((width // 2, height // 2), label, font=font, fill=(255, 255, 255), anchor="mm")
    bar_x = (number * 20) % width  # moves every image, so a stuck display is obvious
    draw.rectangle((bar_x, height - 30, bar_x + 60, height), fill=(255, 200, 0))
    return img.tobytes()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    level = p.add_mutually_exclusive_group(required=True)
    level.add_argument("--noise", type=int, metavar="PERCENT", help="fixed noise area, 0-100")
    level.add_argument("--ramp", action="store_true", help=f"step through {RAMP_LEVELS}")
    p.add_argument("--per-level", type=int, default=20, help="images per level with --ramp (default: 20)")
    p.add_argument("--interval", type=float, default=0.5, help="seconds between images (default: 0.5)")
    p.add_argument("--count", type=int, default=0, help="stop after this many images (default: run until Ctrl+C)")
    args = p.parse_args()
    if args.noise is not None and not 0 <= args.noise <= 100:
        p.error("--noise must be between 0 and 100")

    with AaHmiClient() as client:
        print(f"connected -- sending {client.frame_width}x{client.frame_height} images every "
              f"{args.interval}s (Ctrl+C to quit)")
        font = _load_font(60)
        sent = 0
        try:
            while not args.count or sent < args.count:
                if args.ramp:
                    noise = RAMP_LEVELS[(sent // args.per_level) % len(RAMP_LEVELS)]
                else:
                    noise = args.noise
                sent += 1
                client.send_frame(render(client.frame_width, client.frame_height, sent, noise, font))
                if args.ramp and (sent - 1) % args.per_level == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] image #{sent}: noise {noise}%")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        print(f"sent {sent} images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
