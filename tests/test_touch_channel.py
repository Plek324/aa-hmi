"""touch_channel.py: decoding INPUT_EVENT_INDICATION per aasdk's layout,
and mapping touchscreen coordinates to the client image."""
from aa_hmi.protocol import encode_varint_field
from aa_hmi.touch_channel import TouchAction, TouchEvent, map_to_client, parse_touch_event


def _bytes_field(num, data):
    return bytes([num << 3 | 2, len(data)]) + data


def _touch_message(action, locations, action_index=None, timestamp=123456789):
    """Build an InputEventIndication the way the display does:
    timestamp=1, touch_event=3 { touch_location=1 {x=1, y=2, pointer_id=3}..., action_index=2, touch_action=3 }."""
    touch = b"".join(
        _bytes_field(1, encode_varint_field(1, x) + encode_varint_field(2, y) + encode_varint_field(3, pid))
        for x, y, pid in locations)
    if action_index is not None:
        touch += encode_varint_field(2, action_index)
    touch += encode_varint_field(3, action)
    return encode_varint_field(1, timestamp) + _bytes_field(3, touch)


def test_press_drag_release():
    for action in TouchAction:
        raw = _touch_message(int(action), [(400, 240, 0)])
        event = parse_touch_event(raw)
        assert (event.x, event.y, event.pointer_id, event.action) == (400, 240, 0, action)
        assert event.raw == raw and event.is_structured


def test_messages_end_in_the_byte_pattern_seen_in_old_captures():
    """Raw captures (before the layout was known) ended in 18 00 / 18 02 / 18 01."""
    assert _touch_message(0, [(1, 2, 0)]).endswith(b"\x18\x00")
    assert _touch_message(2, [(1, 2, 0)]).endswith(b"\x18\x02")
    assert _touch_message(1, [(1, 2, 0)]).endswith(b"\x18\x01")


def test_large_coordinates_use_multi_byte_varints():
    event = parse_touch_event(_touch_message(2, [(799, 479, 0)]))
    assert (event.x, event.y) == (799, 479)


def test_action_index_picks_the_location_of_the_finger_that_acted():
    event = parse_touch_event(_touch_message(0, [(10, 20, 0), (300, 200, 1)], action_index=1))
    assert (event.x, event.y, event.pointer_id) == (300, 200, 1)


def test_missing_action_is_press_the_protobuf_default():
    touch = _bytes_field(1, encode_varint_field(1, 5) + encode_varint_field(2, 6))
    event = parse_touch_event(encode_varint_field(1, 1) + _bytes_field(3, touch))
    assert event.action == TouchAction.PRESS and (event.x, event.y) == (5, 6)


def test_non_touch_and_garbage_come_back_raw_only():
    for raw in [b"", b"\xff\xff\xff", encode_varint_field(1, 99),  # no touch_event
                _touch_message(7, [(1, 2, 0)])]:                      # unknown action
        event = parse_touch_event(raw)
        assert event.raw == raw and not event.is_structured


def test_map_to_client_subtracts_the_margin_offset():
    event = TouchEvent(raw=b"", x=409, y=240, pointer_id=0, action=TouchAction.PRESS)
    mapped = map_to_client(event, touch_size=(800, 480), video_size=(800, 480), offset=(9, 20))
    assert (mapped.x, mapped.y) == (400, 220)


def test_map_to_client_touch_in_the_margin_is_outside_the_image():
    event = TouchEvent(raw=b"", x=2, y=5, pointer_id=0, action=TouchAction.PRESS)
    mapped = map_to_client(event, touch_size=(800, 480), video_size=(800, 480), offset=(9, 20))
    assert (mapped.x, mapped.y) == (-7, -15)


def test_map_to_client_scales_when_touchscreen_and_video_differ():
    event = TouchEvent(raw=b"", x=1000, y=600, pointer_id=0, action=TouchAction.DRAG)
    mapped = map_to_client(event, touch_size=(1600, 960), video_size=(800, 480), offset=(0, 0))
    assert (mapped.x, mapped.y) == (500, 300)


def test_map_to_client_leaves_raw_only_events_alone():
    event = TouchEvent(raw=b"\x01")
    assert map_to_client(event, touch_size=(800, 480), video_size=(800, 480), offset=(9, 20)) is event
