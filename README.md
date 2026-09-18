# aa-hmi

Scan for nearby Bluetooth devices, pick one, pair with it, pull its WiFi
credentials over the classic "AA Wireless" RFCOMM bootstrap handshake, and
join that WiFi network — with the device and credentials cached so the
next run just reconnects.

This targets cheap wireless Android-Auto-style head units (the kind that
host their own WiFi AP and hand its credentials to whatever phone asks
over Bluetooth first — see [How it works](#how-it-works)), but nothing
here is tied to Android Auto video specifically. It's meant as a small,
reusable building block for anyone's own project that wants a live
connection to one of these devices.

## Status

**Confirmed working end-to-end** against a real TF811BT motorcycle
display (2026-09-18): Bluetooth scanning, interactive device picker,
pairing, RFCOMM channel discovery (SDP fast-path + brute-force fallback
with protocol validation), the `WifiInfoRequest`/`WifiInfoResponse`
handshake itself (bytes confirmed from a real capture — see
[`docs/protocol-notes.md`](docs/protocol-notes.md#the-full-confirmed-sequence)),
the credential cache, and the `nmcli`-based WiFi connect step.

The confirmed handshake bytes came from exactly one device so far. If you
try this against different head-unit hardware, please open an issue/PR
either way (works identically, or behaves differently) — see
[`docs/capturing-ground-truth.md`](docs/capturing-ground-truth.md) for
the re-verification procedure.

## What this is / isn't

- **Is**: a Bluetooth → WiFi-credential-bootstrap tool, and a WiFi-connect
  convenience on top of that.
- **Isn't**: an Android Auto video client. It doesn't render anything to
  the head unit's screen. For that, see the sibling project
  `aa_pi2display` (a local sibling project, not yet published), which
  impersonates an Android Auto phone to stream custom video to one of
  these displays — and which this tool's WiFi-bootstrap step was
  originally extracted and generalized from.

## Prerequisites

- Linux with [BlueZ](http://www.bluez.org/) (`bluetoothctl`) and
  [NetworkManager](https://networkmanager.dev/) (`nmcli`) — both are
  installed by default on Raspberry Pi OS.
- Python 3.9+. **Zero pip runtime dependencies** — this uses only the
  standard library (including `socket.AF_BLUETOOTH`/`BTPROTO_RFCOMM` for
  the actual RFCOMM connection) and shells out to `bluetoothctl`/`nmcli`
  as separate processes.
- A Bluetooth adapter, obviously, and a target head unit that's powered
  on and discoverable.

## Install

```bash
git clone https://github.com/Plek324/aa-hmi.git
cd aa-hmi
```

**Plain `pip install .` will likely fail** on current Raspberry Pi
OS/Debian with `error: externally-managed-environment` (PEP 668 — the
system Python refuses `pip install` outside a virtual environment,
confirmed on Raspberry Pi OS/Debian Trixie). Pick one:

```bash
# Recommended for regular use: pipx (an isolated venv per app, puts
# `aa-hmi` on your PATH)
sudo apt install pipx   # if you don't already have it
pipx install .

# Or a plain venv, if you'd rather not install pipx
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/aa-hmi run

# To hack on it (editable install):
pipx install -e .   # or: .venv/bin/pip install -e .[dev]
```

Or run it straight from a clone without installing anything — note the
package lives under `src/`, so it needs to be on `PYTHONPATH` explicitly
(a bare `python -m aa_hmi run` from the repo root will fail with `No
module named aa_hmi`):

```bash
PYTHONPATH=src python3 -m aa_hmi run
```

## Usage

### First run (interactive)

```bash
aa-hmi run
```

This scans for nearby Bluetooth devices, lets you pick one, pairs with
it, discovers the right RFCOMM channel, runs the bootstrap handshake, and
joins the resulting WiFi network — caching everything along the way.

```
Bluetooth devices found:
  1. TF811BT_1c64201a  [AA:BB:CC:DD:EE:FF]
  2. My Headphones     [11:22:33:44:55:66]
Select a device [1-2] (or blank to cancel): 1
...
SSID: TF811-1c64201a
Password: 12345678
Connected -- IP address: 192.168.10.42
```

### Subsequent runs

```bash
aa-hmi run
```

With exactly one cached device, this skips the picker and goes straight
to: connect on the known RFCOMM channel, re-run the handshake (still
required every time — the head unit's WiFi listener typically only opens
right after a fresh Bluetooth-triggered session, even with credentials
already known), join WiFi. Noticeably faster than the first run since
channel discovery is skipped too.

Force a fresh scan/pick with `--rescan`, or target a specific cached
device non-interactively with `--device MAC --non-interactive`.

### As a building block

```bash
aa-hmi run --no-wifi-connect --json
```

Runs the Bluetooth bootstrap only, skips the `nmcli` step, and prints the
credentials as JSON on stdout — useful if some other tool wants to do its
own WiFi connection logic.

## CLI reference

```
aa-hmi run [options]        # scan/pair/bootstrap/connect (default command)
  --rescan                  force a fresh BT scan + picker even if cached
  --device MAC              target a specific device
  --channel N                force an RFCOMM channel, skip discovery
  --non-interactive          never prompt; fail if no usable cached device
  --no-wifi-connect          stop after the handshake; print credentials only
  --json                     machine-readable result on stdout
  --radio-coexistence-workaround
                              disconnect WiFi before the BT step (for combo
                              WiFi+BT chips that can't use both at once,
                              e.g. the Raspberry Pi's onboard BCM4345C0)
  --wifi-iface IFACE         WiFi interface to use (default: autodetect)
  --bt-timeout SECONDS       total retry budget for the Bluetooth stage (default: 90)
  --scan-duration SECONDS    BT scan duration (default: 10)
  --config-dir PATH          override the cache directory
  -v, --verbose

aa-hmi list                  show cached devices
aa-hmi forget <mac-or-name>  remove one cached device
aa-hmi forget --all          remove every cached device
```

## How it works

Android-Auto-Wireless-style bootstrap is symmetric by design: the head
unit doesn't authenticate *which* phone is asking, it just answers the
opening Bluetooth handshake with its own WiFi AP credentials
(`WifiInfoResponse`: ssid, key, bssid, security_mode). So a tool that can
speak just the phone's side of that opening handshake gets the
credentials for free, without doing anything resembling a full Android
Auto session. See [`docs/protocol-notes.md`](docs/protocol-notes.md) for
the full wire-level writeup.

## Known hardware quirks

These come from real testing against one specific head unit (a TF811BT
motorcycle display) during the sibling `aa_pi2display` project — your
hardware may behave differently, but these are worth knowing about:

- **Single Bluetooth connection slot.** Many of these head units' classic
  BT radio only holds one connection at a time. If a phone (or anything
  else) is currently connected, bootstrap will fail with confusing
  low-level errors that look like flakiness but really mean "something
  else is holding the radio" — disconnect it there first.
- **Classic Bluetooth can be intermittently unresponsive** for no obvious
  reason, sometimes for several minutes at a stretch. `aa-hmi` retries
  with backoff and a generous default time budget (`--bt-timeout`); if
  it's still failing after a long stretch, try power-cycling the head
  unit.
- **SDP discovery for the AA Wireless RFCOMM channel may not work at
  all** — `aa-hmi` tries it opportunistically but always falls back to a
  validated brute-force scan of channels 1–30.
- **WiFi/Bluetooth radio coexistence** on some client hardware (e.g. the
  Raspberry Pi's onboard combo chip) — active WiFi can starve outbound
  Bluetooth connectivity entirely. `--radio-coexistence-workaround`
  disconnects WiFi before the Bluetooth step as a workaround; it's opt-in
  since it's specific to certain client hardware, not universal.
- **The credential cache stores your WiFi password in plaintext** (with
  `0600` file permissions) at `$XDG_CONFIG_HOME/aa-hmi/devices.json`
  (usually `~/.config/aa-hmi/devices.json`). It's a local convenience
  cache, not a secrets vault.

## Troubleshooting

- **`nmcli` connect fails with "property is missing"**: a stale
  NetworkManager connection profile for that SSID is probably missing
  required security settings. `aa-hmi` already deletes any existing
  profile by that name before connecting — if you still hit this outside
  of `aa-hmi`, try `nmcli connection delete '<ssid>'` manually first.
- **`nmcli` connect fails with "Not authorized to control networking"**:
  a polkit permissions issue, not a bug — confirmed live: a plain SSH
  session with no active console/logind session doesn't get
  NetworkManager's default allow-without-auth policy on some distros,
  even though read-only nmcli commands (like listing WiFi networks) work
  fine from the same session. **Recommended fix (one-time, ~30s, no more
  `sudo` needed afterward):** [`docs/networkmanager-permissions.md`](docs/networkmanager-permissions.md).
  Quick alternative: `sudo $(command -v aa-hmi) run` — plain `sudo
  aa-hmi` won't find the command if you installed via `pipx`, since
  `~/.local/bin` isn't on `sudo`'s `secure_path` (confirmed live too).
- **"no RFCOMM channel ... spoke the expected protocol"**: either the
  head unit isn't actually an AA-Wireless-style device, or its RFCOMM
  channel assignment doesn't match what `aa-hmi` expects (channel numbers
  have been observed to shift over time on the same unit — see
  [`docs/protocol-notes.md`](docs/protocol-notes.md) — a plain rescan
  should find it again).
- **Pairing hangs or fails**: check `bluetoothctl` directly
  (`bluetoothctl pair <MAC>`) to see the raw prompt/error; `aa-hmi`
  auto-confirms passkey/authorization prompts during pairing (see
  [Security notes](#security-notes)) but can't do anything about a head
  unit that's simply not responding — see the single-connection-slot and
  flaky-classic-BT quirks above.

## Contributing

Issues and PRs welcome — especially:
- Completing the `WifiInfoRequest` ground-truth capture (see
  [Status](#status)) against your own hardware.
- Reports of how these quirks (or new ones) show up on different head
  units — the "known hardware quirks" section above is based on exactly
  one device so far.
- A D-Bus discovery backend — see
  [`docs/discovery-backends.md`](docs/discovery-backends.md) for the seam
  it should slot into.

Run the test suite with `pytest` (no hardware needed — see
[`tests/`](tests/); the protocol/cache/retry logic is fully unit-tested,
live-hardware verification is a separate manual process documented
alongside the code).

## Security notes

- Pairing auto-confirms any passkey/service-authorization prompt it sees
  (logged, never silent) — appropriate for the low-security consumer
  head units this targets, not for a general Bluetooth-pairing tool. See
  [`docs/discovery-backends.md`](docs/discovery-backends.md).
- The credential cache holds a plaintext WiFi password on disk (0600
  permissions) — treat it like any other local WiFi credential store, not
  a secrets vault.

## License

MIT — see [`LICENSE`](LICENSE). This project has zero bundled/vendored
runtime dependencies; it shells out to separately-installed system tools
(`bluetoothctl`, `nmcli`) as external processes rather than linking
against them.
