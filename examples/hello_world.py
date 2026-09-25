#!/usr/bin/env python3
"""hello_world.py -- simplest possible aa-hmi client: connect, render one
static frame, send it once, then print any touch events until Ctrl+C.

Start `aa-hmi serve` first (in another terminal, or as a systemd service
-- see deploy/systemd/aa-hmi.service), then run this.

Only imports aa_hmi.ipc.client -- nothing else from aa_hmi. That's the
point: this is what a real, separate content-producing program (like the
AIS-plotter app this whole project was built towards) would do too.
"""
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "src")  # allow running straight from a repo checkout without installing
from aa_hmi.ipc.client import AaHmiClient  # noqa: E402

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Raspberry Pi OS / Debian
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # macOS, if ever run there
]


def _load_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()  # last resort -- tiny, but never fails


def render_hello_world(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), (20, 30, 60))
    draw = ImageDraw.Draw(img)
    font = _load_font(72)
    draw.text((width // 2, height // 2), "Hello, aa-hmi!", font=font, fill=(255, 255, 255), anchor="mm")
    return img.tobytes()


def main() -> int:
    with AaHmiClient() as client:
        print(f"connected -- daemon expects {client.frame_width}x{client.frame_height} "
              f"{client.pixel_format.name} frames")
        frame = render_hello_world(client.frame_width, client.frame_height)
        client.send_frame(frame)
        print("sent one frame -- watch the display. Waiting for touch events (Ctrl+C to quit)...")
        try:
            while True:
                touch = client.poll_touch(timeout=1.0)
                if touch and touch.is_structured:
                    print(f"touch: {touch.action.name} at ({touch.x}, {touch.y})")
                elif touch:
                    print(f"touch (not decoded): raw={touch.raw.hex()}")
        except KeyboardInterrupt:
            print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
