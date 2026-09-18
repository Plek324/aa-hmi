# The TCP/TLS video + touch protocol

This is the writeup of the *other* protocol `aa-hmi` speaks — the TCP/TLS
Android-Auto-Wireless session (port 29880), used once WiFi is already
joined (see [`protocol-notes.md`](protocol-notes.md) for the Bluetooth
bootstrap that gets you there). Ported from the sibling
[`aa_pi2display`](https://github.com/Plek324/aa_pi2display) project's
proof-of-concept (`aa_session.py`, `live_demo.py`), which is where every
fact below was actually confirmed against real hardware — this doc is the
reference, `src/aa_hmi/video_session.py`/`video_protocol.py` is the
implementation.

## Wire framing

Every frame, before and after TLS: **4-byte header — `channel:u8,
flags:u8, length:u16` (big-endian) — then `length` bytes of body.**
Pre-TLS, body = 2-byte `message_id` + plaintext payload. Post-TLS, body =
one raw TLS record (the message_id lives inside the decrypted plaintext
instead). This is a *different* framing from the Bluetooth RFCOMM side's
(`protocol.py`) — see `video_protocol.py`.

`flags=0x0F` marks a channel-open frame (confirmed from a real phone's
own capture); `flags=0x0B` is normal per-channel traffic thereafter.

## Session sequence

1. TCP connect to `display_ip:29880`.
2. Display → phone: `VersionRequest` (content ignored).
3. Phone → display: `VersionResponse` (msg id 2, **plaintext**, channel 0,
   flags `0x03`) — major=1, minor=7, status=0.
4. **TLS 1.2 handshake**, driven via `ssl.MemoryBIO` — the display is the
   TLS *client*, we're the TLS *server* (confirmed real, not a guess).
   **Critical, proven-the-hard-way bug**: you must flush
   `outgoing.read()` *after* a successful `do_handshake()` call, and only
   `break`/return after that flush. A naive `do_handshake(); break` never
   sends your own final `Finished` message — the display just sits
   waiting forever and never sends `AuthComplete`. Looks exactly like
   "handshake succeeded but display went silent," not an obvious bug.
   `video_session.py`'s `_do_tls_handshake` preserves this ordering
   exactly; don't "simplify" it during a future refactor without rereading
   this paragraph.
5. Display → phone: `AuthComplete` (plaintext, last unencrypted message,
   content ignored).
6. Phone → display: `ServiceDiscoveryRequest` (msg id 5, encrypted).
7. Open the video channel (channel 3): `AV_SETUP_REQUEST` (msg id
   `0x8000`, flags `0x0F`), then `VIDEO_FOCUS_REQUEST` (msg id `0x8007`,
   flags `0x0B`, three varint fields: `field1=0, field2=focus_mode
   (1=FOCUSED/2=UNFOCUSED), field3=1`).
8. Stream `AV_MEDIA_WITH_TIMESTAMP_INDICATION` frames (msg id `0x0000`,
   channel 3, flags `0x0B`). Plaintext = 2-byte msg_id + 8-byte
   big-endian **stream-relative** timestamp (NOT wall-clock — the display
   has no RTC, always boots to a fixed `2024-01-01 12:00`) + Annex-B NAL
   bytes.
9. Clean shutdown, always: `VIDEO_FOCUS_REQUEST(UNFOCUSED)` →
   `AV_STOP_INDICATION` (msg id `0x8002`) → `MSG_SHUTDOWN_REQUEST` (msg id
   `0x000F`). `video_session.py`'s `close()` runs this in every code path,
   including on a session that never fully opened.

## The two proven wire-level bugs, both ported forward

- **`split_tls_records`**: a single `tls_obj.write()` can produce
  ciphertext spanning multiple TLS records (16KB plaintext max per RFC
  5246). The display's decoder can't handle multiple TLS records bundled
  under one wire frame — confirmed to cause a black screen. Every
  encrypted send in `video_session.py` splits and sends each real TLS
  record as its own wire frame. Cheap insurance, keep it even though the
  new low-fps use case makes it less likely to matter.
- **H.264 access-unit boundaries**: NALs are grouped into access units by
  closing at every VCL slice NAL (type 1 or 5) — see `encoder.py`. This is
  **deliberately not spec-correct** AUD-based grouping; that was tried
  once and made a black-screen problem worse. Keep the non-spec-correct
  version; it's what's proven to work.

## Why per-frame ffmpeg invocation, not a persistent pipe

`aa_pi2display`'s `live_demo.py` used a persistent ffmpeg subprocess +
reader thread + frame-pacing loop for continuous 5fps rendering, and hit
an unresolved black-screen bug under that load (several real causes found
and fixed — sparse keyframes, frame-pacing catch-up bursts, a silently-dying
reader thread on an empty NAL — but the black screen persisted, root cause
never found).

This project's actual target rate is 1 frame per 1–10 seconds, never
continuous video. At that rate, `encoder.py` spawns a fresh `ffmpeg`
process per frame instead: no persistent pipe, no frame-pacing loop to
race, no long-lived reader thread to silently die. Every invocation
encodes exactly one input image, so it's always a keyframe by
construction — nothing resembling the old bug categories can occur here.
Process-spawn overhead (tens of ms on a Pi 4) is irrelevant against a
multi-second budget.

## Touch: proven vs. not proven — a correction

An earlier doc (`aa_pi2display`'s `real-protocol-findings.md`) claims
touch decode is *"fully working... decodes exactly per the aasdk
proto."* **That claim is inaccurate about field-level decode
specifically.** What's actually proven: channel 1
(`INPUT_EVENT_INDICATION`, msg id `0x8001`) opens the same way every
other channel does (best-guess empty open frame, `flags=0x0F` — content
doesn't seem to matter, same as channel 3's open), and real touch
gestures genuinely arrive as raw decrypted bytes with an observable
pattern in the trailing byte: `...1800` for PRESS, `...1802` for DRAG,
`...1801` for RELEASE. What was **never actually implemented or tested**
anywhere in that project: the real protobuf field numbers for
`touch_location.x`, `touch_location.y`, `pointer_id`, `action_index` —
only raw hex was ever logged. The real field layout exists only in an
`aasdk` proto checkout on the Pi (`~/aa-project/aasdk`), not in any file
available to this project.

`aa-hmi`'s `touch_channel.py` reflects this honestly: it relays raw bytes
today (`TouchEvent.raw`), with `x`/`y`/`pointer_id`/`action` declared but
always `None`, guarded by a `TOUCH_GROUND_TRUTH_CONFIRMED = False` flag —
same pattern as `messages.GROUND_TRUTH_CONFIRMED` on the Bluetooth side.

### Follow-up task: capturing the real field layout

1. On the Pi, `~/aa-project/aasdk` should have the real proto
   definitions — find `InputEvent`/`TouchEvent`'s field numbers there.
2. Alternatively (or to cross-check), capture+decrypt a real drag gesture
   the same way `protocol-notes.md` decoded a real `WifiInfoResponse` —
   `aa-hmi serve -v` plus a TLS keylog would give you the raw bytes; walk
   the tag/wire-type/length bytes by hand.
3. Fill in `touch_channel.parse_touch_event()`, flip
   `TOUCH_GROUND_TRUTH_CONFIRMED` to `True`, update
   `tests/test_touch_channel.py` (it currently asserts the placeholder
   behavior deliberately — that assertion needs to change alongside the
   fix, not be silently left stale).
4. Bump `ipc/protocol.py`'s `TOUCH` message `schema` byte and start
   setting `has_structured=1` — old clients keep working unmodified since
   they only ever read `.raw`.

### Known UX risk, not solved, no known fix

Touching the screen brings up the **display's own native popup menu** —
never seen with a real phone connected. This suggests the display runs
its own overlay UI, independent of and possibly covering whatever's
streamed to it. No suppression method is known. If this can't be worked
around, it may limit how usable a genuinely interactive touch UI can be
on this specific hardware — worth confirming/investigating before relying
on touch for anything beyond simple confirmation taps.

## Confirmed: the Bluetooth link must stay OPEN through the TCP connect, not just have been used

New finding while bringing up `aa-hmi serve` (2026-09-18), refining what
`aa_pi2display` had already established. It wasn't enough for `aa-hmi`'s
RFCOMM bootstrap to just receive `WifiInfoResponse` and disconnect (which
is exactly what `aa-hmi run`'s narrower credentials-only scope correctly
does) — the display's TCP video port kept refusing connections
(`ConnectionRefusedError`) even immediately after successfully joining
its WiFi network. Two things turned out to matter, confirmed by direct
experiment:

1. **Two more RFCOMM messages matter**: `WifiStartResponse` (msg id 7,
   confirmed real payload `0x1800`) and `WifiConnectStatus` (msg id 6,
   confirmed real payload `0x0800`), sent immediately after
   `WifiInfoResponse` — these are exactly what a real working
   `aa-proxy-rs` probe session sends next (see
   `tests/fixtures/ground_truth_probe_session.txt`), which `aa-hmi`'s own
   credential-only bootstrap had deliberately never needed to send.
2. **The RFCOMM/Bluetooth connection must still be OPEN when the TCP
   connect happens** — sending those two messages on an *already-closed*
   socket was NOT sufficient; confirmed directly by holding a diagnostic
   RFCOMM connection open in one shell while probing the TCP port from
   another, which succeeded, versus every attempt that closed the RFCOMM
   socket first, which got `ConnectionRefusedError`. This matches
   `aa-proxy-rs`'s own observed behavior — its probe process keeps
   running (and thus keeps the connection alive) for up to 45s while
   `run_session.sh` proceeds to reconnect WiFi and start the TCP session;
   that was previously read as "just how the probe happens to behave,"
   not as a real requirement, until this confirmed it is one.

`bootstrap.confirm_wifi_connected()` sends the two messages;
`orchestrate.bootstrap_wifi_info(..., confirm_connected=True)` (used only
by `daemon.py`, never by `aa-hmi run`) keeps the socket open and returns
it instead of closing it. `daemon.py` holds that socket open for the
whole video-session lifetime (out of caution — only confirmed it must be
open *at* TCP-connect time, not proven how much longer beyond that is
actually required) and closes it alongside the video session on any
reconnect or shutdown.

## Open question: does one Bluetooth trigger hold a session open indefinitely?

Separately from the above (which is now resolved): what's still **not**
confirmed either way is whether an *already-established* video session
can be held indefinitely with the Bluetooth link eventually closed, or
whether something requires periodic re-arming even on a live session.
Every test in `aa_pi2display`'s history ran a bounded-duration session
(stream one clip, or `--duration N`, then exit) — nothing has ever tested
multi-hour persistence, and `daemon.py` currently just holds the RFCOMM
link open for as long as the video session lives rather than testing
whether it could be closed sooner.

`daemon.py` handles this the safe way regardless of the real answer: any
drop of the video session (TLS error, TCP EOF, a dead reader thread)
triggers a **full** reconnect — device resolution, Bluetooth re-trigger,
WiFi re-bootstrap, fresh TLS handshake — from scratch, every time. This
is always correct, just potentially wasteful if a lighter-weight
reconnect would have worked.

### Soak-test task (not yet run)

Run `examples/clock.py` against real hardware continuously for 2+ hours
(longer is better). `aa-hmi serve -v` logs every reconnect with a cause.
Report back here with what actually happened: zero reconnects the whole
time (session genuinely persists), reconnects at some roughly periodic
interval (suggests a real timeout to characterize), or reconnects
correlating with something else observable (WiFi hiccups, etc). Any of
these outcomes is useful data — update this section once you have it.

## Troubleshooting: a stale kernel-level Bluetooth connection that `bluetoothctl` can't see

Found live while repeatedly starting/stopping `aa-hmi serve` for testing
(2026-09-18): after enough rapid connect/disconnect cycles against the
same head unit, RFCOMM channel discovery started failing on *every*
channel with `Host is down` / timeouts — matching the documented flaky-BT
quirk, but this time it didn't clear on its own. `bluetoothctl info
<MAC>` reported `Connected: no`, yet `hcitool con` showed a real,
stuck ACL connection to that exact MAC (`state 5 lm CENTRAL`) — a
kernel/BlueZ-level connection that the higher-level D-Bus API wasn't
reporting at all. `bluetoothctl disconnect <MAC>` claimed success but did
**not** actually clear it; only a full adapter reset did:

```bash
sudo hciconfig hci0 down
sudo hciconfig hci0 up
```

After that, `hcitool con` showed no connections and a fresh `aa-hmi
serve` connected cleanly again. If you hit `Host is down`/timeout errors
on every single RFCOMM channel (not just some), check `hcitool con` (or
`sudo hcitool con` if permissions require it) for a stale connection
before assuming it's ordinary flakiness or reaching for a full head-unit
power cycle — the adapter reset above is faster and non-disruptive to the
head unit itself.
