# Staged live-hardware verification: daemon mode

Unit-testable logic is covered by `pytest` (protocol framing, encoder
invocation, cert generation, IPC framing/server behavior — see
`tests/`). This is the manual, staged process for everything that
actually needs the real display, mirroring
[`capturing-ground-truth.md`](capturing-ground-truth.md)'s approach for
the Bluetooth side. Run these roughly in order — each builds on the last.

## 0. Prerequisites

- A paired, cached device (`aa-hmi run` once, successfully) — or be
  ready to answer the interactive picker.
- `ffmpeg` and `openssl` installed on the Pi.
- Pillow installed if you'll run the example clients (`pip install
  pillow` or `pip install -e .[examples]`).

## 1. TCP connect + TLS handshake

```bash
aa-hmi serve -v
```

Watch the log for, in order: `connecting to ...`, `<- VersionRequest?`,
`-> sent VersionResponse`, and critically **`*** TLS HANDSHAKE
COMPLETE ***`** followed by `<- AuthComplete` — not just the handshake
line alone. If the log stops right after "TLS HANDSHAKE COMPLETE" and
never reaches AuthComplete, that's the exact proven bug
(`video-protocol-notes.md`'s MemoryBIO-flush ordering) recurring —
something reordered the flush-after-handshake logic.

## 2. Video channel open

Still in the same `-v` log: confirm no errors after the handshake — the
video channel open (`AV_SETUP_REQUEST`/`VIDEO_FOCUS_REQUEST`) doesn't log
much on success by design (the reader thread just drains ACKs silently),
so "no errors, log keeps running" is the expected good outcome here.

## 3. Single-frame encode → real display render

This is the one most worth watching with your own eyes — it's the
architecture change from the old persistent-pipe design.

```bash
python3 examples/hello_world.py
```

Confirm: "Hello, aa-hmi!" actually appears on the physical panel. If it
doesn't:
- Check `aa-hmi serve`'s log for an `EncoderError` (ffmpeg missing/failed)
  or a `VideoSessionError` around the `send_frame` call.
- If ffmpeg ran without error but nothing appeared, capture+decrypt the
  session (same TLS-keylog approach `aa_pi2display` used) and compare the
  sent frame's bytes against a known-working static clip's frame shape.

## 4. Touch channel opens, raw bytes observed

With `hello_world.py` still running (it prints touch events), tap the
display. Confirm:
- A line like `touch: raw=...` appears in the client's output.
- Separately, note (don't need to fix) whether the display's own native
  popup menu appears — this is the known, unsolved UX risk from
  `video-protocol-notes.md`.

## 5. Full IPC path, repeating frames

```bash
python3 examples/clock.py --interval 5
```

Confirm the clock updates on the display roughly every 5 seconds, and
that stopping/restarting `clock.py` (Ctrl+C, rerun) doesn't require
restarting `aa-hmi serve` — the IPC server should just accept the new
connection.

## 6. Soak test

Leave `examples/clock.py` running for hours against `aa-hmi serve -v`
(inside `tmux` or as a systemd service, so it doesn't depend on your SSH
client staying awake). Watch for a frozen display, or `session attempt
failed` / `reconnecting in Ns...` log lines. Reference result
(2026-09-25): 11h22m at 2 images/s, 80,701 images, no freeze, no
reconnect.

## 7. Clean shutdown

```bash
# in the terminal running `aa-hmi serve`:
Ctrl+C  (or: kill -TERM <pid> from elsewhere)
```

Confirm the log shows the full shutdown sequence (`requesting video focus
UNFOCUSED...` → `sending clean ShutdownRequest` → `video session closed`)
and the display returns to its own idle/home screen **without needing a
power cycle**. If the display is left showing a frozen frame, that's the
same "unclean termination" failure mode documented in the sibling
project — check what actually happened in the log around shutdown.

## Deploying for this verification

Same `pscp`/SSH workflow used throughout this project's development:

```bash
tar -czf /tmp/aa-hmi.tar.gz --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.git' .
pscp -pw '<password>' -batch /tmp/aa-hmi.tar.gz peter@<pi-ip>:/tmp/aa-hmi.tar.gz
plink -ssh -batch -pw '<password>' peter@<pi-ip> \
  'rm -rf /tmp/aa-hmi-live && mkdir -p /tmp/aa-hmi-live && tar -xzf /tmp/aa-hmi.tar.gz -C /tmp/aa-hmi-live'
```

Then run with `PYTHONPATH=src python3 -m aa_hmi serve -v` (or install
into a venv/pipx as usual — see the README).
