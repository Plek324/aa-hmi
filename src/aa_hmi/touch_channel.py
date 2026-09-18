"""touch_channel.py -- touch input, PLACEHOLDER decode.

What's actually proven: channel 1 (INPUT_EVENT_INDICATION, msg_id 0x8001)
opens the same way every other channel does, and real touch gestures DO
arrive as raw decrypted bytes with an observable pattern -- a trailing
byte of `...1800` for PRESS, `...1802` for DRAG, `...1801` for RELEASE,
confirmed against real touch-and-drag gestures via the aa_pi2display
sibling project's capture+decrypt pipeline.

What's NOT proven, despite an earlier doc (aa_pi2display's
real-protocol-findings.md) claiming otherwise: the actual protobuf field
numbers for touch_location.x, touch_location.y, pointer_id, and
action_index were never implemented or tested anywhere -- only raw hex
was ever logged. The real field layout exists only in an `aasdk` proto
checkout on the Pi (~/aa-project/aasdk), not in any file available to
this project. That earlier doc's "decodes exactly per the aasdk proto...
fully working" claim is INACCURATE about field-level decode specifically
-- channel-open and raw-bytes-arriving is what's actually confirmed.

So: this module relays raw bytes now. TouchAction's PRESS/DRAG/RELEASE
values are pre-declared from the *observed byte pattern* above, even
though the field *numbers* that carry them aren't confirmed -- mirroring
messages.py's existing separation of "observed values" from "confirmed
wire encoding" (see SecurityMode there). Real (x, y) decode is a
follow-up task: capture a real drag gesture, decode it byte-for-byte the
way protocol-notes.md decoded WifiInfoResponse, fill in
parse_touch_event(), flip TOUCH_GROUND_TRUTH_CONFIRMED to True -- same
pattern as messages.GROUND_TRUTH_CONFIRMED. See
docs/video-protocol-notes.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

TOUCH_GROUND_TRUTH_CONFIRMED = False


class TouchAction(IntEnum):
    """Values as OBSERVED in the trailing byte of real captured gestures
    -- NOT confirmed as the real protobuf enum wire values, since the
    field layout itself isn't known yet. Do not treat these as
    authoritative until TOUCH_GROUND_TRUTH_CONFIRMED is True."""
    PRESS = 0
    RELEASE = 1
    DRAG = 2


@dataclass
class TouchEvent:
    raw: bytes
    schema: int = 1  # matches ipc/protocol.py's TOUCH message schema byte
    x: int | None = None          # TODO(touch-ground-truth): field number unknown
    y: int | None = None          # TODO(touch-ground-truth): field number unknown
    pointer_id: int | None = None  # TODO(touch-ground-truth): field number unknown
    action: TouchAction | None = None  # TODO(touch-ground-truth): field number unknown

    @property
    def is_structured(self) -> bool:
        return self.action is not None


def parse_touch_event(raw: bytes) -> TouchEvent:
    """Placeholder: always returns a raw-only TouchEvent. Intentionally
    does not attempt to guess field numbers -- see module docstring.
    Guarded by TOUCH_GROUND_TRUTH_CONFIRMED so it's obvious in code (and
    in tests/test_touch_channel.py, which asserts on this) the day
    someone captures the real layout and this needs to change."""
    assert not TOUCH_GROUND_TRUTH_CONFIRMED, (
        "TOUCH_GROUND_TRUTH_CONFIRMED is True but parse_touch_event() was never "
        "updated to actually decode x/y/pointer_id/action -- fix this function, "
        "then this assertion becomes dead code and can be removed."
    )
    return TouchEvent(raw=raw)
