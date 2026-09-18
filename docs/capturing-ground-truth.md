# Capturing ground truth: `WifiInfoRequest`'s real bytes

`aa-hmi` refuses to guess what bytes to send for `WifiInfoRequest` at a
real head unit — see [`protocol-notes.md`](protocol-notes.md) for why.
This is the procedure to get 100%-certain ground truth instead, the same
"capture real bytes, decode, replicate" method the sibling
[`aa_pi2display`](https://github.com/REPLACE_ME/aa_pi2display) project
used successfully throughout its own reverse-engineering.

You'll need [`aa-proxy-rs`](https://github.com/aa-proxy/aa-proxy-rs)
built and already working in `probe` mode against your head unit (see
that project's own docs, or `aa_pi2display`'s
`extracting-wifi-credentials.md`, which walks through this exact setup).

## Steps

1. Confirm `/etc/aa-proxy-rs/config.toml` has:
   ```toml
   bt_wireless_proxy = true
   bt_wireless_proxy_mode = "probe"
   bt_wireless_proxy_hu_mac = "<your head unit's Bluetooth MAC>"
   debug = true
   ```
   `debug = true` is essential — it turns on `aa-proxy-rs`'s built-in
   `ProxyFrameSniffer`, which logs every raw Bluetooth frame's hex payload
   in both directions.

2. Run the probe and capture its full output:
   ```bash
   sudo timeout 45 aa-proxy-rs -c /etc/aa-proxy-rs/config.toml 2>&1 | tee /tmp/probe_debug.log
   ```

3. Extract **every** line that looks like a `ProxyFrameSniffer` frame log
   — both head-unit→proxy and proxy→head-unit directions, in order. Only
   one such line has been documented anywhere so far (the inbound
   `WifiInfoResponse`):
   ```
   HU -> POC frame id=3 (WifiInfoResponse) len=47 payload=<hex bytes>
   ```
   The *outbound* direction's exact log format hasn't been confirmed yet
   — look for the mirror-image tag (likely something like `POC -> HU`) and
   note the exact wording you actually see, since it may differ from this
   guess.

4. Save the full ordered sequence (every frame, both directions, in the
   order they were logged) into `tests/fixtures/ground_truth_probe_session.txt`
   in this repo. Include a header comment with the date, your head unit's
   model, and the `aa-proxy-rs` version/commit you used.

5. Hand-decode each **outbound** frame the same way the existing
   `WifiInfoResponse` example was decoded in `protocol-notes.md` (walk the
   protobuf tag/wire-type/length bytes). You're looking for:
   - Is there a `WifiVersionRequest` (msg_id 4) sent before anything else?
     If so, what's in its payload?
   - What's actually in the `WifiInfoRequest` (msg_id 2) payload — is it
     really empty (length 0), or does it carry fields?

6. Write your findings into `protocol-notes.md`'s "What's still
   unconfirmed" section (replacing it with confirmed facts), then update
   `src/aa_hmi/messages.py`:
   - Fill in `WIFI_INFO_REQUEST_PAYLOAD` (and `WIFI_VERSION_REQUEST_PAYLOAD`
     if applicable) with the real bytes.
   - Flip `GROUND_TRUTH_CONFIRMED = True`.
   - Update/remove the guardrail test in `tests/test_messages_roundtrip.py`
     (`test_ground_truth_not_confirmed_by_default`) to match.

7. Test end-to-end against your real head unit: `aa-hmi run --rescan -v`
   and confirm you get real credentials back, matching what you already
   know from `aa-proxy-rs`'s own probe output.

Please open a PR with your findings — even a partial capture (e.g. "here's
the outbound frame log format, still need the byte-level decode") is
useful for the next person working on this.
