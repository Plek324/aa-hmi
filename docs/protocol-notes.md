# The RFCOMM WiFi-bootstrap protocol

This is the writeup of the Bluetooth-side protocol `aa-hmi` speaks — the
"AA Wireless" bootstrap that hands out a head unit's own WiFi AP
credentials over classic Bluetooth RFCOMM, before any WiFi/TCP connection
exists at all. It's a much smaller protocol than the actual Android Auto
video session (see the sibling project,
[aa_pi2display](https://github.com/REPLACE_ME/aa_pi2display), for that).

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
Wireless service. Validate with the actual handshake, not just a
successful `connect()`.

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

## `WifiInfoRequest` — what's still unconfirmed

**The exact outbound bytes to elicit a `WifiInfoResponse` are not yet
confirmed.** No `.proto` file for `WifiInfoRequest` is published anywhere
found so far, which suggests (but does not confirm) an empty body. Until
someone completes the capture procedure in
[`capturing-ground-truth.md`](capturing-ground-truth.md) against a real
head unit and fills in the real bytes, `src/aa_hmi/messages.py` keeps
`GROUND_TRUTH_CONFIRMED = False` and `bootstrap.py` refuses to guess bytes
at a real device.

If/when that capture is done, update this section with:
- Whether a `WifiVersionRequest`/`WifiVersionResponse` exchange happens
  first, and its exact payload format.
- `WifiInfoRequest`'s exact payload bytes (or confirmation that it's
  genuinely empty).
- Any other quirks observed (e.g. does the head unit send anything
  unprompted before a request is sent?).
