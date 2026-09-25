#!/usr/bin/env python3
"""touch_test.py -- see touch input on the display itself.

Shows five targets (four corners and the centre). Touch them: a green
circle marks where a finger went down (PRESS), yellow dots follow it
while it stays down (DRAG), and a red cross marks where it lifted
(RELEASE). If the marks land under your finger, touch coordinates are
decoded and mapped correctly. The top line shows the last event and how
many of each arrived. Tap CLEAR to wipe the marks.

The console prints every PRESS and RELEASE with coordinates, the number
of DRAG events in between, and the raw bytes of anything that couldn't
be decoded.

Start `aa-hmi serve` first. Ctrl+C to quit.
"""
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "src")  # allow running straight from a repo checkout without installing
from aa_hmi.ipc.client import AaHmiClient  # noqa: E402

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
MIN_SEND_INTERVAL = 0.1  # seconds; a finger streams DRAG events much faster than that


def _load_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


class TouchCanvas:
    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.font = _load_font(18)
        self.targets = [(40, 60), (width - 40, 60), (40, height - 40), (width - 40, height - 40),
                        (width // 2, height // 2)]
        self.clear_box = (width // 2 - 60, height - 60, width // 2 + 60, height - 15)
        self.counts = {"PRESS": 0, "DRAG": 0, "RELEASE": 0}
        self.status = "touch a target"
        self.marks = Image.new("RGB", (width, height), (0, 0, 0))
        self._marks_draw = ImageDraw.Draw(self.marks)

    def handle(self, action: str, x: int, y: int) -> None:
        self.counts[action] += 1
        self.status = f"{action} at ({x}, {y})"
        cb = self.clear_box
        if action == "PRESS" and cb[0] <= x <= cb[2] and cb[1] <= y <= cb[3]:
            self.marks.paste((0, 0, 0), (0, 0, self.width, self.height))
            self.counts = {k: 0 for k in self.counts}
            self.status = "cleared"
            return
        d = self._marks_draw
        if action == "PRESS":
            d.ellipse((x - 14, y - 14, x + 14, y + 14), outline=(0, 230, 0), width=3)
        elif action == "DRAG":
            d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(255, 220, 0))
        else:
            d.line((x - 10, y - 10, x + 10, y + 10), fill=(255, 40, 40), width=3)
            d.line((x - 10, y + 10, x + 10, y - 10), fill=(255, 40, 40), width=3)

    def render(self) -> bytes:
        img = Image.new("RGB", (self.width, self.height), (15, 20, 40))
        mask = self.marks.convert("L").point(lambda v: 255 if v else 0)
        draw = ImageDraw.Draw(img)
        grey = (150, 160, 190)
        for tx, ty in self.targets:
            draw.ellipse((tx - 20, ty - 20, tx + 20, ty + 20), outline=grey, width=2)
            draw.line((tx - 28, ty, tx + 28, ty), fill=grey)
            draw.line((tx, ty - 28, tx, ty + 28), fill=grey)
            label_y = ty + 36 if ty < self.height // 2 else ty - 36
            draw.text((tx, label_y), f"{tx},{ty}", font=self.font, fill=grey, anchor="mm")
        draw.rectangle(self.clear_box, outline=(220, 220, 220), width=2)
        draw.text(((self.clear_box[0] + self.clear_box[2]) // 2, (self.clear_box[1] + self.clear_box[3]) // 2),
                  "CLEAR", font=self.font, fill=(220, 220, 220), anchor="mm")
        img.paste(self.marks, (0, 0), mask)
        counts = "  ".join(f"{k.lower()} {v}" for k, v in self.counts.items())
        draw.text((self.width // 2, 20), f"{self.status}    [{counts}]", font=self.font,
                  fill=(255, 255, 255), anchor="mm")
        return img.tobytes()


def main() -> int:
    with AaHmiClient() as client:
        canvas = TouchCanvas(client.frame_width, client.frame_height)
        print(f"connected -- {client.frame_width}x{client.frame_height}; touch the targets (Ctrl+C to quit)")
        client.send_frame(canvas.render())
        dirty, last_send, drags = False, time.monotonic(), 0
        try:
            while True:
                touch = client.poll_touch(timeout=0.05)
                if touch is not None:
                    if touch.is_structured:
                        action = touch.action.name
                        canvas.handle(action, touch.x, touch.y)
                        dirty = True
                        if action == "DRAG":
                            drags += 1
                        else:
                            if action == "RELEASE":
                                print(f"  ...{drags} DRAG events")
                            print(f"{action} at ({touch.x}, {touch.y})")
                            drags = 0
                    else:
                        print(f"touch (not decoded): raw={touch.raw.hex()}")
                if dirty and time.monotonic() - last_send >= MIN_SEND_INTERVAL:
                    client.send_frame(canvas.render())
                    dirty, last_send = False, time.monotonic()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
