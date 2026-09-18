# Contributing

Thanks for considering it — this is a small hobby/reverse-engineering
project and outside contributions are genuinely welcome, especially from
anyone with different head-unit hardware to test against.

## Setup

```bash
git clone https://github.com/Plek324/aa-hmi.git
cd aa-hmi
pip install -e .[dev]
pytest
```

The test suite needs no real Bluetooth/WiFi hardware — it covers the
protocol codec, the credential cache, the retry helper, and the RFCOMM
channel-candidate ordering, all as pure/mocked logic. Anything that
genuinely needs live hardware (the actual scan/pair/bootstrap/connect
flow) is verified manually — see the staged checklist in this repo's
planning notes, or just run `aa-hmi run -v` against your own head unit
and watch the log output.

## Where to start

- **Highest value right now**: completing the `WifiInfoRequest`
  ground-truth capture — see [`docs/capturing-ground-truth.md`](docs/capturing-ground-truth.md).
  This unblocks the tool actually working end-to-end.
- Reports of how the "known hardware quirks" in the README show up (or
  don't) on head units other than the one this was built against.
- A D-Bus discovery backend, if `bluetoothctl` genuinely doesn't work for
  your setup — see [`docs/discovery-backends.md`](docs/discovery-backends.md).

## Style

- Stdlib-only for the protocol/transport layer — no protobuf library, no
  PyBluez. If you're adding something that needs a real dependency,
  explain why in the PR; it's not automatically off the table, just worth
  discussing (see `docs/discovery-backends.md` for how the D-Bus tradeoff
  was reasoned about).
- Parse real device responses **leniently** — never add strict
  proto-enum validation or `required`-field enforcement to
  `protocol.py`/`messages.py`. Real hardware has already been observed
  sending an out-of-spec enum value and omitting a `required` field; code
  that would reject that is a regression, not a correctness improvement.
- New protocol findings belong in `docs/protocol-notes.md`, with a real
  captured example where possible — same spirit as the sibling
  `aa_pi2display` project's `real-protocol-findings.md`.

## Reporting a new head unit's quirks

Even a short PR to the README's "known hardware quirks" section (or a new
entry noting something *doesn't* apply to your unit) is useful — this
project's quirk list is currently based on testing against exactly one
device.
