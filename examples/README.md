# Example clients

Reference programs showing how to use `aa-hmi`'s local socket protocol
(`docs/ipc-protocol.md`) via the Python client library
(`aa_hmi.ipc.client.AaHmiClient`). All import *only* that library plus
Pillow for rendering -- nothing else from `aa_hmi` -- proving the client
library is the whole interface a separate program needs.

## Running

1. Start the daemon first (leave it running in its own terminal, or as a
   systemd service -- see `../deploy/systemd/aa-hmi.service`):
   ```bash
   aa-hmi serve
   ```
2. Then, from this repo's root, run either example:
   ```bash
   python3 examples/hello_world.py
   python3 examples/clock.py                 # --interval SECONDS, default 5
   python3 examples/stress.py --ramp         # large-image test, see below
   ```

## What each one shows

- **`hello_world.py`** — the minimal case: connect, render one static
  frame, send it once, print any touch events (raw bytes only -- touch
  decode is a placeholder, see `../src/aa_hmi/touch_channel.py`) until
  Ctrl+C.
- **`clock.py`** — a repeating frame at a configurable interval. Also a
  handy soak-test tool: leave it running for hours against real hardware
  (`aa-hmi serve` logs every reconnect it does on its own side).

- **`stress.py`** — deliberately hard-to-compress images (part random
  noise), to test images over 16KB, which go to the display in several
  pieces. `--noise PERCENT` for a fixed level, `--ramp` to step through
  0-100%. Measured on a Pi 4: 0% is ~10KB (1 piece), 2% ~21KB (2),
  10% ~58KB (4), 40% ~187KB (12), 100% ~445KB (28). Each image shows its
  number and noise level, and a bar moves along the bottom, so a stuck or
  corrupted image is easy to spot. Run `aa-hmi serve -v` to see the
  sizes and piece counts in its log.

All require Pillow (`pip install pillow` / `pip install -e .[dev]` from
the repo root, or run from wherever your interpreter has Pillow available).
