"""touch_channel.py -- decode touch events from the display.

Touch arrives on channel 1 as INPUT_EVENT_INDICATION (msg id 0x8001).
Field layout from aasdk's protos (aasdk_proto/InputEventIndicationMessage,
TouchEventData, TouchLocationData, TouchActionEnum):

    InputEventIndication { timestamp = 1; disp_channel = 2; touch_event = 3; ... }
    TouchEvent           { repeated touch_location = 1; action_index = 2; touch_action = 3 }
    TouchLocation        { x = 1; y = 2; pointer_id = 3 }
    TouchAction          { PRESS = 0; RELEASE = 1; DRAG = 2 }

This matches what was seen in raw captures before the layout was known:
messages ending in `18 00` / `18 02` / `18 01` = touch_action PRESS /
DRAG / RELEASE. While a finger is down, the display streams DRAG events.

Coordinates arrive in the display's touchscreen space (800x480 on the
Podofo, from its ServiceDiscoveryResponse). map_to_client() converts
them to the client program's image, which sits inside the video's
margins (see display_info.py and docs/video-protocol-notes.md).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum

from .protocol import decode_fields, get_varint

_IEI_TOUCH_EVENT = 3
_TE_LOCATION, _TE_ACTION_INDEX, _TE_ACTION = 1, 2, 3
_TL_X, _TL_Y, _TL_POINTER = 1, 2, 3


class TouchAction(IntEnum):
    PRESS = 0
    RELEASE = 1
    DRAG = 2


@dataclass
class TouchEvent:
    raw: bytes
    schema: int = 1  # matches ipc/protocol.py's TOUCH message schema byte
    x: int | None = None
    y: int | None = None
    pointer_id: int | None = None
    action: TouchAction | None = None

    @property
    def is_structured(self) -> bool:
        return self.action is not None and self.x is not None and self.y is not None


def parse_touch_event(raw: bytes) -> TouchEvent:
    """Decode one INPUT_EVENT_INDICATION body (after the msg id). Anything
    that isn't a recognisable touch (button events, unknown actions,
    garbage) comes back raw-only; never raises."""
    event = TouchEvent(raw=raw)
    try:
        top = decode_fields(raw)
        touch = next((f.value for f in top.get(_IEI_TOUCH_EVENT, []) if f.wire_type == 2), None)
        if touch is None:
            return event
        fields = decode_fields(bytes(touch))
        locations = [decode_fields(bytes(f.value)) for f in fields.get(_TE_LOCATION, []) if f.wire_type == 2]
        if not locations:
            return event
        # A missing touch_action is the protobuf default, PRESS.
        action_value = get_varint(fields, _TE_ACTION) or 0
        if action_value not in TouchAction._value2member_map_:
            return event
        index = get_varint(fields, _TE_ACTION_INDEX) or 0
        location = locations[index] if index < len(locations) else locations[0]
        x, y = get_varint(location, _TL_X), get_varint(location, _TL_Y)
        if x is None or y is None:
            return event
        return replace(event, x=x, y=y, pointer_id=get_varint(location, _TL_POINTER) or 0,
                       action=TouchAction(action_value))
    except Exception:  # noqa: BLE001 -- a malformed event must never break touch relaying
        return event


def map_to_client(event: TouchEvent, *, touch_size: tuple[int, int], video_size: tuple[int, int],
                  offset: tuple[int, int]) -> TouchEvent:
    """Touchscreen coordinates -> client image coordinates: scale from the
    touchscreen's size to the video's (the same on the Podofo), then
    subtract where the client image sits in the video. A touch in the
    margin area can give coordinates outside the client image (negative,
    or >= its size); they're passed on as-is, the IPC format is signed."""
    if not event.is_structured:
        return event
    x = event.x * video_size[0] // touch_size[0] - offset[0]
    y = event.y * video_size[1] // touch_size[1] - offset[1]
    return replace(event, x=max(-32768, min(32767, x)), y=max(-32768, min(32767, y)))
