# Changelog

## 0.1.0 (unreleased)

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
