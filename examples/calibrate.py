#!/usr/bin/env python3
"""calibrate.py -- a test pattern to find out which part of the image the
display actually shows.

Coloured frames sit at fixed distances from each edge of the image:

    red 0 px, orange 10, yellow 20, green 30, cyan 40, blue 50,
    magenta 60, white 70

Each frame is 4 px thick. Look at each edge of the display and note the
first colour you can see there: that tells you how many pixels the
display cuts off at that edge (e.g. first colour green on the left means
30-39 px are lost on the left). Two tick rulers (a tick every 10 px, a
label every 100 px) show whether the image is also scaled.

Start `aa-hmi serve` first. The image is resent every few seconds, so it
survives a reconnect. Ctrl+C to quit.
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
FRAMES = [  # (distance from edge in px, name, colour)
    (0, "red", (255, 0, 0)),
    (10, "orange", (255, 140, 0)),
    (20, "yellow", (255, 255, 0)),
    (30, "green", (0, 220, 0)),
    (40, "cyan", (0, 255, 255)),
    (50, "blue", (40, 90, 255)),
    (60, "magenta", (255, 0, 255)),
    (70, "white", (255, 255, 255)),
]
FRAME_THICKNESS = 4


def _load_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    for distance, _name, colour in FRAMES:
        draw.rectangle((distance, distance, width - 1 - distance, height - 1 - distance),
                       outline=colour, width=FRAME_THICKNESS)

    small, big = _load_font(14), _load_font(22)
    cx, cy = width // 2, height // 2
    grey = (170, 170, 170)
    # Horizontal ruler through the middle: tick every 10 px, label every 50.
    for x in range(80, width - 80 + 1, 10):
        tall = x % 50 == 0
        draw.line((x, cy - (12 if tall else 5), x, cy + (12 if tall else 5)), fill=grey)
        if x % 100 == 0:
            draw.text((x, cy + 16), str(x), font=small, fill=grey, anchor="mt")
    # Vertical ruler, left of the centred text.
    rx = 140
    for y in range(80, height - 130 + 1, 10):  # stops above the legend
        tall = y % 50 == 0
        draw.line((rx - (12 if tall else 5), y, rx + (12 if tall else 5), y), fill=grey)
        if y % 100 == 0 and abs(y - cy) > 20:
            draw.text((rx + 16, y), str(y), font=small, fill=grey, anchor="lm")

    draw.text((cx, 100), f"image {width} x {height}", font=big, fill=(255, 255, 255), anchor="mm")
    legend = "  ".join(f"{name} {d}" for d, name, _ in FRAMES)
    draw.text((cx, height - 110), "first colour visible at an edge = px cut off there:",
              font=small, fill=grey, anchor="mm")
    draw.text((cx, height - 90), legend, font=small, fill=grey, anchor="mm")
    return img.tobytes()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=float, default=3.0, help="seconds between resends (default: 3)")
    args = p.parse_args()

    with AaHmiClient() as client:
        frame = render(client.frame_width, client.frame_height)
        print(f"connected -- showing a {client.frame_width}x{client.frame_height} test pattern (Ctrl+C to quit)")
        print("frames from the edge: " + ", ".join(f"{name} {d}px" for d, name, _ in FRAMES))
        try:
            while True:
                client.send_frame(frame)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
