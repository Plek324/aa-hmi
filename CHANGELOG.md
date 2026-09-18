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
- **Known gap**: `WifiInfoRequest`'s exact outbound bytes are not yet
  confirmed against a real capture — see `docs/capturing-ground-truth.md`.
  `bootstrap.py` refuses to guess bytes at a real device until this is
  done.
