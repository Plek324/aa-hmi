# The local client ↔ daemon socket protocol

This is the wire spec for the local socket `aa-hmi serve` exposes to any
separate content-producing program (in any language — this doc has no
Python in it on purpose). If you're writing a Python client, you almost
certainly want `aa_hmi.ipc.client.AaHmiClient` instead of implementing
this by hand — see [`../examples/`](../examples/) for working examples.
This doc exists for anyone implementing a client in another language, and
as the source of truth `src/aa_hmi/ipc/protocol.py` is checked against.

## Transport and socket location

A Unix domain stream socket, local machine only. Default path:
`$XDG_RUNTIME_DIR/aa-hmi/video.sock`, falling back to
`/tmp/aa-hmi/video.sock` if `$XDG_RUNTIME_DIR` is unset — override with
`aa-hmi serve --socket-path PATH`. The socket file is created `0600`
(owner read/write only) inside a `0700` directory — this is
local-machine-only by design, not a multi-user-safe IPC mechanism.

**v1 supports exactly one connected client at a time.** A second
connection attempt while one is already attached receives an `ERROR`
message (`CLIENT_ALREADY_CONNECTED`) and is closed. Multi-client support
would need `TOUCH` fan-out to every connection and a policy for
concurrent `FRAME` sends — not solved, noted here as a real follow-up.

## Framing

```
+------------------+----------------+---------------------+
| length (u32, BE) | msg_type (u8)  | payload (length - 1) |
+------------------+----------------+---------------------+
```

`length` covers `msg_type` plus everything after it. u32, not u16 — a
single 800×480 RGB24 frame is `800 × 480 × 3 = 1,152,000` bytes, already
far past a u16 field's 65535-byte ceiling.

All multi-byte integers in this spec are **big-endian**.

## Message types

| Value | Name | Direction |
|---|---|---|
| 1 | `HELLO` | client → server |
| 2 | `HELLO_ACK` | server → client |
| 3 | `FRAME` | client → server |
| 4 | `TOUCH` | server → client |
| 5 | `PING` | either direction |
| 6 | `PONG` | either direction |
| 7 | `ERROR` | server → client |

## Handshake

The client's **first message must be `HELLO`**; anything else first gets
`ERROR` (`HELLO_REQUIRED`) and disconnect.

**`HELLO`** (client → server):
```
protocol_version : u16
client_name_len  : u16
client_name      : client_name_len bytes, UTF-8
```

**`HELLO_ACK`** (server → client, sent once, immediately after a valid
`HELLO`):
```
protocol_version : u16   -- the server's version; today always 1
frame_width      : u16   -- e.g. 782
frame_height     : u16   -- e.g. 440
pixel_format     : u8    -- 1 = RGB24 (row-major, 3 bytes/pixel, no padding)
```
The server tells the client the geometry to use, rather than the client
assuming a fixed size. It's the display's *visible* area: the video
size minus its margins (daemon flags `--video-size` and `--margins`;
782×440 by default, inside 800×480 video) — the daemon centres the
client's image in the video. This is what lets a different display
panel be supported without a protocol version bump. **A client must use
exactly this geometry for every `FRAME` it sends.**

Current protocol version: **1**.

## `FRAME` (client → server)

```
width        : u16
height       : u16
pixel_format : u8   -- must be 1 (RGB24) in v1
reserved     : u8   -- must be 0; reserved for future flags
pixel_data   : width * height * bytes_per_pixel bytes
```

`width`/`height`/`pixel_format` must exactly match what `HELLO_ACK`
reported — the server rejects a mismatch with `ERROR`
(`WRONG_GEOMETRY`) rather than silently rescaling. This header exists so
a future server version can accept other geometries/formats without
breaking the wire format for everyone else.

There's no acknowledgement for a `FRAME` beyond an `ERROR` on rejection —
if you don't get an `ERROR`, assume it was accepted. Send frames as
infrequently as your content actually changes; there is no minimum or
maximum rate enforced by the protocol itself (the target use case is
roughly 1 frame per 1–10 seconds, never continuous video — see
[`video-protocol-notes.md`](video-protocol-notes.md) for why that matters
to how the server encodes frames).

## `TOUCH` (server → client)

```
schema          : u8    -- touch payload format version, today always 1
raw_len         : u16
raw             : raw_len bytes  -- the undecoded payload from the display's touch channel
has_structured  : u8    -- 0 or 1
  -- only present if has_structured == 1:
  x             : i16
  y             : i16
  pointer_id    : u8
  action        : u8    -- 0=PRESS, 1=RELEASE, 2=DRAG
```

**Today, `has_structured` is always `0`** — the real protobuf field
layout for touch coordinates was never confirmed against this hardware
(see [`video-protocol-notes.md`](video-protocol-notes.md)'s "Touch:
proven vs. not proven" section). Only `raw` is populated. This is a
deliberate forward-compatible design, not an incomplete one: a future
server version that *has* confirmed the real field layout can start
setting `has_structured=1` and filling in real coordinates, `schema`
bumps to reflect that, and **old clients keep working unmodified** since
they only ever read `raw` and the trailing bytes are new, not
repurposed. New clients should check `has_structured` before trusting
`x`/`y`/`pointer_id`/`action`.

There's no flow control here either — if you're not reading, you miss
touch events that occurred while you were away, the same as a real
touchscreen driver gives no buffering guarantee.

## `PING` / `PONG`

Empty payload, either direction. The server sends periodic `PING`s as an
idle keepalive (Unix domain sockets don't give you TCP-style keepalive
detection for free) — a client that wants to detect a hung/dead server
can watch for these, or send its own `PING` and expect a `PONG` back.

## `ERROR` (server → client)

```
code        : u8
message_len : u16
message     : message_len bytes, UTF-8, human-readable
```

| code | meaning |
|---|---|
| 0 | `UNKNOWN` |
| 1 | `PROTOCOL_VERSION_MISMATCH` |
| 2 | `HELLO_REQUIRED` |
| 3 | `WRONG_GEOMETRY` |
| 4 | `CLIENT_ALREADY_CONNECTED` |
| 5 | `INTERNAL` |

An `ERROR` doesn't necessarily mean the connection is being closed —
check which code it is. `HELLO_REQUIRED` and `CLIENT_ALREADY_CONNECTED`
are always immediately followed by the server closing the connection.
`WRONG_GEOMETRY` and `INTERNAL` (from a malformed `FRAME`) leave the
connection open — fix your next message and keep going.

## What happens while the daemon is reconnecting to the display

`FRAME` messages sent while `aa-hmi serve` is between video sessions
(reconnecting to the display after a drop — see
[`video-protocol-notes.md`](video-protocol-notes.md)) are currently just
silently dropped, not `ERROR`'d — the daemon doesn't want to disconnect a
perfectly good IPC client just because the display connection is
temporarily down. There's no `STATUS` message in v1 to let a client ask
"is the display session currently up?" — a real gap for a client that
wants to show its own "no display connected" state; noted as a follow-up,
not solved.
