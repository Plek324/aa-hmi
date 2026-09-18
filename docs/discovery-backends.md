# Discovery backend: why `bluetoothctl`, and how to add another

`src/aa_hmi/discovery.py` talks to Bluetooth (scanning + pairing) by
shelling out to `bluetoothctl`. This page explains why, and how to add a
different backend if you need one.

## Why `bluetoothctl`

Three realistic options were considered:

- **PyBluez** (`bluetooth.discover_devices()`) — simple discovery API,
  but effectively unmaintained and often painful to install against
  current Python/BlueZ versions. It also doesn't help with *pairing* at
  all — classic Bluetooth pairing still goes through BlueZ's agent API
  regardless of what discovers the device, so PyBluez would only replace
  a third of the problem while adding real installation risk.
- **BlueZ D-Bus** (`dbus-python` / `pydbus`) — the "correct" long-term
  robust approach: a structured API, proper agent registration for
  pairing, no screen-scraping. But it's a real added dependency
  (`dbus-python` needs system build headers), and meaningfully more code.
  Good candidate for a second backend, not the MVP.
- **`bluetoothctl` via `subprocess`** (chosen) — zero extra Python
  dependency. BlueZ (and `bluetoothctl` with it) is already present on
  any Raspberry Pi OS install, and is already what the sibling
  `aa_pi2display` project's own docs tell you to use manually
  (`bluetoothctl pair <MAC>`). Modern `bluetoothctl` supports one-shot
  non-interactive subcommands for the common path (`power on`,
  `--timeout N scan on`, `devices`, `pair <MAC>`, `trust <MAC>`, `info
  <MAC>`, `remove <MAC>`).

The one gap: a one-shot `bluetoothctl pair` can hang if BlueZ's default
agent needs a passkey confirmation ("Confirm passkey NNNNNN (yes/no)") or
a service authorization prompt — those only show up on bluetoothctl's
interactive REPL. `discovery.py`'s `BluetoothCtl.pair()` falls back to a
small interactive-REPL driver (`_pair_interactive`) in that case, which
auto-answers `yes` to any of those prompts. This is a deliberate,
documented tradeoff (see the README's Security notes) — these are
low-security consumer head units, not a threat model where an
unattended confirmation prompt matters. Every auto-confirmed prompt is
logged at INFO level, never silent.

## Adding a backend

`discovery.py` intentionally exposes a small surface:

```python
class SomeBackend:
    def power_on(self) -> None: ...
    def scan(self, duration_s: float) -> list[BtDevice]: ...
    def is_paired(self, mac: str) -> bool: ...
    def pair(self, mac: str) -> bool: ...
    def remove(self, mac: str) -> None: ...
```

`cli.py` only depends on this surface (via the `BluetoothCtl` type hint,
which nothing enforces structurally today — a `Protocol` would be a
reasonable follow-up if a second backend actually gets built). To add a
D-Bus-based backend:

1. Implement the methods above in a new `discovery_dbus.py`.
2. Add a way to select it (an env var or `--discovery-backend` CLI flag
   would both be reasonable — not built yet since there's only one
   backend today).
3. `BtDevice` (mac + name) is the only shared data type — reuse it.

Contributions welcome, especially from anyone who's hit a real case where
`bluetoothctl` genuinely isn't available (e.g. a minimal/embedded BlueZ
setup without the CLI tools installed).
