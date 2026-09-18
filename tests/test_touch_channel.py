"""Deliberately written so this fails loudly the day someone adds real
touch-field decoding without updating BOTH parse_touch_event() and this
test -- see touch_channel.py's module docstring for why that's the point."""
from aa_hmi.touch_channel import TOUCH_GROUND_TRUTH_CONFIRMED, TouchAction, parse_touch_event


def test_touch_ground_truth_is_not_confirmed_yet():
    """Guardrail: this documents the actual current state (placeholder,
    not real field decode). If this ever needs to become True, it must
    happen alongside parse_touch_event() actually being rewritten -- see
    that function's own assertion, which would then start firing."""
    assert TOUCH_GROUND_TRUTH_CONFIRMED is False


def test_parse_touch_event_is_raw_only():
    event = parse_touch_event(b"\x01\x02\x03\x18\x00")
    assert event.raw == b"\x01\x02\x03\x18\x00"
    assert event.x is None
    assert event.y is None
    assert event.pointer_id is None
    assert event.action is None
    assert event.is_structured is False


def test_touch_action_values_match_observed_byte_pattern():
    """PRESS/DRAG/RELEASE values as OBSERVED in real captured gestures'
    trailing bytes (...1800/1802/1801) -- see touch_channel.py. These are
    NOT confirmed protobuf field encodings, just documented observed
    values, kept distinct from "confirmed wire encoding" the same way
    messages.SecurityMode is."""
    assert TouchAction.PRESS == 0
    assert TouchAction.RELEASE == 1
    assert TouchAction.DRAG == 2
