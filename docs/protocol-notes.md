# The RFCOMM WiFi-bootstrap protocol

This is the writeup of the Bluetooth-side protocol `aa-hmi` speaks — the
"AA Wireless" bootstrap that hands out a head unit's own WiFi AP
credentials over classic Bluetooth RFCOMM, before any WiFi/TCP connection
exists at all. It's a much smaller protocol than the actual Android Auto
video session (see the sibling project,
`aa_pi2display` (a local sibling project, not yet published), for that).

## Where this comes from

Confirmed from [`aa-proxy-rs`](https://github.com/aa-proxy/aa-proxy-rs)'s
public source (`src/bluetooth.rs`, `src/protos/`), a Rust project that
implements this same handshake (among other things) — and cross-checked
against a real capture from a TF811BT motorcycle AA display, decoded by
hand and documented in `aa_pi2display`'s `extracting-wifi-credentials.md`.
Not everything is confirmed yet — see "What's still unconfirmed" below.

## Transport

Classic Bluetooth RFCOMM (BR/EDR, not BLE). The head unit must already be
paired over classic Bluetooth before an RFCOMM connection will work at
all (`bluetoothctl pair <MAC>`).

**Which RFCOMM channel?** SDP (Service Discovery Protocol) is supposed to
answer this, and this tool tries it opportunistically via `sdptool
browse` if available. In practice, SDP has been found completely broken
on at least one real unit tested (empty response for every query, every
time) — so `aa-hmi` always falls back to brute-force scanning channels
1–30, connecting to each and checking whether it actually speaks this
protocol. **Accepting a raw connection is not sufficient evidence** — on
the one unit this has been tested against, channels 4, 7, 12 and 15 *all*
accepted a raw RFCOMM connect, but only channel 4 was the real AA
Wireless service in that earlier test. Validate with the actual
handshake, not just a successful `connect()`.

**The channel number isn't necessarily stable, either.** A later live run
of `aa-hmi` itself against the exact same unit (same MAC, same BlueZ
adapter, same day) found the real service on **channel 3**, not 4 —
channel 4 had started refusing connections outright. Nothing about the
head unit was reconfigured between these two findings; the most likely
explanation is that BlueZ/the head unit's own RFCOMM channel assignment
for a service can shift across Bluetooth stack restarts or re-pairs, not
that it's a fixed per-device constant. This is a direct, practical
argument for always validating via brute force rather than hardcoding a
channel number once found and trusting it forever (which is what a
static `aa-proxy-rs` config with `bt_wireless_proxy_hu_channel` pinned to
one value does) — `aa-hmi`'s cache stores the last-known-good channel as
a fast-path optimization, but always falls back to rediscovery if it
stops working (see `rfcomm.py`/`bootstrap.py`), rather than treating the
cached value as permanent.

## Framing

Every message: **2-byte big-endian payload length, then 2-byte
big-endian message-id, then the payload** (a protobuf-encoded message,
in the cases documented below). No channel/flags byte — unlike the
TCP/TLS side of the full Android Auto protocol (see
`aa_pi2display/real-protocol-findings.md`), RFCOMM is already its own
dedicated logical channel, so there's nothing to multiplex here.

```
+------------------+------------------+------------------+
| length (2 bytes) | msg_id (2 bytes) | payload (length)  |
+------------------+------------------+------------------+
```

## Message IDs

```
WifiStartRequest    = 1
WifiInfoRequest      = 2
WifiInfoResponse     = 3
WifiVersionRequest   = 4
WifiVersionResponse  = 5
WifiConnectStatus    = 6
WifiStartResponse    = 7
WifiPingRequest      = 8
WifiPingResponse     = 9
WifiSetupInfo        = 11
```

`aa-hmi`'s own scope only needs `WifiInfoRequest`/`WifiInfoResponse` (and
possibly a `WifiVersionRequest`/`WifiVersionResponse` exchange first — see
below). `WifiStartRequest`/`WifiStartResponse` are for a *different*
bootstrap direction (the phone hosting its own hotspot and telling the
head unit where to dial in) — not needed when the head unit hosts its own
AP, which is the case this tool targets. The ping/keepalive messages are
for long-lived sessions and aren't needed just to fetch credentials.

## `WifiInfoResponse` (confirmed)

proto2, `LITE_RUNTIME`:

```proto
message WifiInfoResponse {
    required string ssid = 1;
    required string key = 2;
    required string bssid = 3;
    required SecurityMode security_mode = 4;
    required AccessPointType access_point_type = 5;
}

enum SecurityMode {
    UNKNOWN_SECURITY_MODE = 0; OPEN = 1; WEP_64 = 2; WEP_128 = 3;
    WPA_PERSONAL = 4; WPA2_PERSONAL = 8; WPA_WPA2_PERSONAL = 12;
    WPA_ENTERPRISE = 20; WPA2_ENTERPRISE = 24; WPA_WPA2_ENTERPRISE = 28;
}

enum AccessPointType { STATIC = 0; DYNAMIC = 1; }
```

A real captured response (TF811BT display), hex:

```
0a0e54463831312d3163363432303161120831323334353637381a1136383a38663a63393a62333a32343a33372005
```

| bytes | meaning |
|---|---|
| `0a 0e` | field 1 (ssid), length 14 |
| `54 46 ... 61` | `"TF811-1c64201a"` |
| `12 08` | field 2 (key), length 8 |
| `31 ... 38` | `"12345678"` |
| `1a 11` | field 3 (bssid), length 17 |
| `36 38 ... 37` | `"68:8f:c9:b3:24:37"` |
| `20 05` | field 4 (security_mode), varint value `5` |

**Field 4's value (`5`) is not a valid `SecurityMode` enum value**, and
field 5 (`access_point_type`) is **entirely absent** despite the proto
marking both fields `required`. This is decisive for how `aa-hmi` parses
these messages: **leniently**. `src/aa_hmi/protocol.py`'s `decode_fields`
walks raw tag/wire-type/value triples and never validates an enum or
requires a field to be present — a strict protobuf library would reject
this exact real-world message. Real devices are the ground truth here,
not the spec.

## The full confirmed sequence

Captured 2026-09-18 against a real TF811BT display, via `aa-proxy-rs`'s
`debug = true` probe-mode logging (procedure:
[`capturing-ground-truth.md`](capturing-ground-truth.md); raw log:
[`../tests/fixtures/ground_truth_probe_session.txt`](../tests/fixtures/ground_truth_probe_session.txt)).
Reproduced identically across two separate RFCOMM connections in the same
capture session:

1. **RFCOMM connects.**
2. **HU → POC: `WifiVersionRequest` (id `4`), sent unprompted**, before
   anything is requested — contains the head unit's supported WiFi
   channel list (100 bytes: major/minor version + a long list of 2.4/5GHz
   channel numbers) and empty `car_make`/`hu_model`/etc fields.
   **No response is required** — the real `aa-proxy-rs` probe doesn't
   send a `WifiVersionResponse` either, it just reads this one frame,
   logs a "wasn't what I expected but continuing anyway" warning
   internally, and moves straight on. `aa-hmi`'s `bootstrap.py` drains
   and discards whatever arrives here (if anything — this is a
   best-effort drain, not a required step, in case some other head unit
   doesn't send anything unprompted).
3. **POC → HU: `WifiInfoRequest` (id `2`), CONFIRMED EMPTY body
   (`len=0`)** — this was a plausible guess before this capture, now
   verified byte-for-byte from the real outbound frame log.
4. **HU → POC: `WifiInfoResponse` (id `3`)** — the credentials, exactly
   the payload already decoded above. Notably, **`aa-proxy-rs`'s own
   strict protobuf parser fails to parse this exact real response**
   (`Message 'WifiInfoResponse' is missing required fields`) — direct,
   independent confirmation (from a completely different implementation)
   that lenient parsing is the right call here, not a shortcut this
   project happened to need.

What follows in a real session — `WifiStartResponse` (id `7`) and
`WifiConnectStatus` (id `6`), both sent POC → HU — is the proxy telling
the head unit "I'm now connected and ready." That's out of `aa-hmi`'s
scope (see "Message IDs" above): this tool stops at step 4, once it has
the credentials, and uses them to join the WiFi network directly via
`nmcli` rather than continuing the in-band handshake.
