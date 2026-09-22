# Changelog

## Unreleased (since 0.2.0)

Two more real bugs found live (2026-09-22), reported by a user testing
`aa-hmi serve` with `examples/clock.py`:

- **`Ctrl+C` left the Pi connected to the display's WiFi**, breaking the
  *next* `aa-hmi serve` start (the documented WiFi/Bluetooth
  radio-coexistence issue on the Pi's onboard combo chip -- active WiFi
  starves outbound Bluetooth). `daemon.py` now disconnects and forgets
  the display's WiFi connection on final shutdown (Ctrl+C/SIGTERM, not
  between automatic reconnect cycles) -- mirrors `aa_pi2display`'s
  original `run_session.sh`, which always did this and `daemon.py` had
  simply never picked up.
- **More robust fix for the same underlying issue, regardless of cause**:
  `--radio-coexistence-workaround` now defaults to **on** for `aa-hmi
  serve` specifically (still off for `aa-hmi run`, via the new
  `--no-radio-coexistence-workaround` opt-out). `serve`'s own reconnect
  loop redoes the full Bluetooth bootstrap on every drop, so without
  this, radio contention would recur on every single reconnect, not just
  after a graceful-shutdown edge case -- and it also protects against
  leftover WiFi state from any cause (a crash, `kill -9`, not just a
  normal Ctrl+C), not only the specific case just fixed above. Verified
  live: starting `serve` with WiFi already stuck connected from a prior
  run (the exact reported failure) now self-heals automatically with no
  manual cleanup.
- **New finding, documented, root cause still unconfirmed**: while
  testing the above fixes with many rapid `aa-hmi serve` start/stop
  cycles in quick succession, something went fully unresponsive at the
  IP layer (100% ping loss to the head unit's gateway address) even
  though Bluetooth/RFCOMM bootstrap kept succeeding on every single
  attempt. Originally guessed to be the head unit's own AP wedging --
  **contradicted the same day**: a user hit this independently, a head
  unit power cycle did NOT fix it, a Raspberry Pi reboot did. Points at
  the Pi's own combo-chip driver/radio state more than the head unit.
  Root cause still not nailed down -- the Pi's `journalctl` wasn't
  configured for persistent storage, so the evidence from the bad state
  was lost on reboot; now fixed (`Storage=persistent` in
  `/etc/systemd/journald.conf`, a Pi-side config change, not part of this
  repo) so a future occurrence can actually be diagnosed. See
  `docs/video-protocol-notes.md`'s troubleshooting section: don't
  rapid-cycle `serve` against real hardware while developing/testing.

## 0.2.0

Daemon mode: `aa-hmi serve` — the full display-server piece, on top of
0.1.0's Bluetooth/WiFi bootstrap. This is what turns `aa-hmi` into a
replacement for the sibling `aa_pi2display` project's proof-of-concept
scripts rather than a separate tool consumed by them.

- `video_session.py` — the TCP/TLS Android-Auto-Wireless video session as
  a reusable class, ported from `aa_pi2display`'s `aa_session.py` (proven
  handshake sequence, including the critical MemoryBIO-flush-after-handshake
  ordering fix, preserved exactly) but restructured for a long-running
  daemon: explicit state, a background reader thread, thread-safe sends.
- `encoder.py` — per-frame `ffmpeg` invocation (spawn, encode one image to
  one keyframe, exit) instead of porting the sibling project's persistent-pipe
  architecture, which had an unresolved black-screen bug under continuous
  rendering. At this project's actual target rate (1 frame per 1–10s),
  per-frame spawning is simpler and structurally avoids most of that bug's
  root-cause categories. See `docs/video-protocol-notes.md`.
- `touch_channel.py` — touch input, honestly represented as a placeholder:
  channel-open and raw-bytes-arriving are proven, but real x/y/pointer_id
  field decode was never actually implemented anywhere (an earlier doc's
  "fully working" claim was inaccurate) — this release relays raw bytes
  and documents the real follow-up task, rather than guessing.
- A new local IPC protocol (`ipc/`) — Unix domain socket, `docs/ipc-protocol.md`
  has the full language-agnostic wire spec — so any separate program can
  push rendered frames in and receive touch events out, plus a reference
  Python client library (`aa_hmi.ipc.client.AaHmiClient`).
- `daemon.py` — orchestrates bootstrap → cert → video session → IPC
  server, with automatic full reconnection (Bluetooth re-trigger included)
  on any video-session drop — the safe default given a still-open question
  about whether one Bluetooth trigger holds a session open indefinitely
  (see `docs/video-protocol-notes.md`'s soak-test task).
- `orchestrate.py` — the device-selection/WiFi-bootstrap logic extracted
  out of `cli.py` (previously private functions there) so both `run` and
  the new `serve` daemon share it without `daemon.py` importing `cli.py`.
- Two example client apps, `examples/hello_world.py` and `examples/clock.py`,
  using only the reference client library + Pillow.
- `deploy/systemd/aa-hmi.service` — a starting template for unattended
  operation (with an explicit warning about the nmcli/polkit permission
  issue, which is worse under systemd than under a plain SSH session).
- New docs: `docs/video-protocol-notes.md`, `docs/ipc-protocol.md`,
  `docs/video-live-verification.md`.
- **Live-verified end-to-end, visually confirmed** (2026-09-18) against
  the real display: full bootstrap → TCP/TLS handshake → video/touch
  channels → `examples/hello_world.py` sending a real frame over the IPC
  socket — "Hello, aa-hmi!" seen rendered on the physical panel. Two real
  bugs found and fixed in the process:
  - The RFCOMM/Bluetooth link has to stay **open** through the TCP
    connect (not just have sent the right messages) for the display's
    video port to accept a connection — `WifiStartResponse`/`WifiConnectStatus`
    (bytes confirmed from the ground-truth capture) now get sent, and
    `orchestrate.bootstrap_wifi_info(..., confirm_connected=True)` keeps
    the socket open and returns it instead of closing it, for `daemon.py`
    to hold through the video session's lifetime. See
    `docs/video-protocol-notes.md`.
  - `SIGTERM` during an in-progress Bluetooth bootstrap retry didn't
    actually stop the daemon promptly — it kept grinding through the full
    retry/backoff budget (which, combined with a flaky-BT episode, could
    mean minutes). `retry_with_backoff` gained an optional `cancel_event`
    (checked before each attempt and during backoff waits, via
    `retry.RetryCancelledError`), threaded through
    `orchestrate.bootstrap_wifi_info` from `daemon.py`'s own shutdown
    `stop_event`. Doesn't interrupt an attempt already in flight, but
    closes the large majority of the gap.
  - Also documented: a stale kernel-level Bluetooth ACL connection that
    `bluetoothctl`/D-Bus doesn't report at all, found live after repeated
    connect/disconnect testing cycles — `hcitool con` shows it,
    `sudo hciconfig hci0 down && ... up` clears it. See
    `docs/video-protocol-notes.md`'s new Troubleshooting section.
- **Still not done**: the multi-hour soak test for the session-persistence
  question, and broader multi-cycle reconnect testing — see
  `docs/video-live-verification.md`'s remaining staged checklist items.

## 0.1.0

Initial release.

- Bluetooth device scan + interactive picker, pairing via `bluetoothctl`.
- RFCOMM channel discovery: opportunistic SDP fast-path, brute-force
  1–30 fallback, validated against the real protocol (not just "accepts a
  connection").
- Lenient RFCOMM protobuf-wire-format decoder for `WifiInfoResponse`
  (tolerates the out-of-spec `security_mode` value and missing
  `access_point_type` field observed on real hardware).
- `nmcli`-based WiFi connect, including the stale-connection-profile
  workaround.
- JSON credential cache (`$XDG_CONFIG_HOME/aa-hmi/devices.json`, 0600
  permissions) for instant reconnects.
- CLI: `run` (default), `list`, `forget`.
- `WifiInfoRequest`'s exact outbound bytes confirmed against a real
  capture (TF811BT display, 2026-09-18) — see
  `docs/capturing-ground-truth.md` and
  `tests/fixtures/ground_truth_probe_session.txt`. The capture also
  revealed the head unit sends an unsolicited `WifiVersionRequest` before
  anything is asked, which `bootstrap.py` now drains before proceeding.
  Confirmed working end-to-end against real hardware.
- Two more fixes found via that live run: `delete_stale_profile` was
  checking for the wrong nmcli error wording ("no such connection" vs.
  the real "unknown connection"), and the credential cache resolved to
  `/root/.config` instead of the invoking user's home when run under
  `sudo` (needed on some setups to work around nmcli's polkit "Not
  authorized to control networking" — now documented in Troubleshooting,
  with a clear hint printed when it happens). The cache now correctly
  resolves the real user's home via `$SUDO_USER` in that case, and
  `save()` chowns the cache file/directory back to `$SUDO_UID:$SUDO_GID`
  so it stays readable/writable by that user's later non-sudo runs too.
- Fixed the README's install instructions, which didn't actually work as
  written (found via a real user trying them): plain `pip install .`
  fails with `externally-managed-environment` on current Raspberry Pi
  OS/Debian (PEP 668), and the documented "just run from source" command
  (`python -m aa_hmi run`) failed with `No module named aa_hmi` since the
  package lives under `src/` and wasn't on `PYTHONPATH`. Now documents a
  `pipx`/venv install and `PYTHONPATH=src python3 -m aa_hmi run`, both
  verified working on a real Raspberry Pi from a fresh clone.
- `sudo aa-hmi` reported as still not working after the above: turned out
  to be two more real issues, both confirmed live and now fixed/documented:
  a `pipx` install puts `aa-hmi` in `~/.local/bin`, which isn't on
  `sudo`'s `secure_path` (`sudo aa-hmi` → "command not found" — use `sudo
  $(command -v aa-hmi)` instead); and, more importantly, the underlying
  polkit "Not authorized" issue has a proper permanent fix that avoids
  needing `sudo` at all — a one-time local polkit rule, documented with
  exact commands in the new `docs/networkmanager-permissions.md` and
  verified live (real `nmcli` WiFi connect succeeding as a plain user
  over SSH, no `sudo`, immediately after adding the rule). `errors.py`'s
  `HINT_NMCLI_NOT_AUTHORIZED` now points here first.
