# Example clients

Reference programs showing how to use `aa-hmi`'s local socket protocol
(`docs/ipc-protocol.md`) via the Python client library
(`aa_hmi.ipc.client.AaHmiClient`). Both import *only* that library plus
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
   ```

## What each one shows

- **`hello_world.py`** — the minimal case: connect, render one static
  frame, send it once, print any touch events (raw bytes only -- touch
  decode is a placeholder, see `../src/aa_hmi/touch_channel.py`) until
  Ctrl+C.
- **`clock.py`** — a repeating frame at a configurable interval. Also a
  handy soak-test tool: leave it running for hours against real hardware
  (`aa-hmi serve` logs every reconnect it does on its own side).

Both require Pillow (`pip install pillow` / `pip install -e .[dev]` from
the repo root, or run from wherever your interpreter has Pillow available).
