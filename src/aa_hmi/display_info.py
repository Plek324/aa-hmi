"""display_info.py -- decode what the display says about itself.

Right after the TLS handshake the display answers our
ServiceDiscoveryRequest with a ServiceDiscoveryResponse (control channel,
msg id 0x0006): its channels, and for the video channel the resolution,
frame rate, margins and density it wants; for the input channel the
touchscreen size. Field numbers follow aasdk's protos, checked against a
real Podofo/TF811BT capture (tests/fixtures/service_discovery_response_podofo.hex):

    video 800x480 @30fps, margins 18 x 40, 140 dpi; touchscreen 800x480

Margins are Android Auto's way of saying "the edges of this video may not
be visible": a phone keeps its UI inside the central
(width - margin_width) x (height - margin_height) area.
"""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import decode_fields, get_varint

# aasdk VideoResolution enum
VIDEO_RESOLUTIONS = {1: (800, 480), 2: (1280, 720), 3: (1920, 1080)}
# aasdk VideoFPS enum
VIDEO_FPS = {1: 60, 2: 30}

# ServiceDiscoveryResponse fields
_SDR_CHANNEL, _SDR_MODEL, _SDR_YEAR, _SDR_MANUFACTURER, _SDR_HU_MODEL, _SDR_SW_VERSION = 1, 3, 4, 7, 8, 10
# Channel descriptor fields
_CH_ID, _CH_MEDIA_SINK, _CH_INPUT = 1, 3, 4
# Media sink: stream type (3 = video) and video configs
_SINK_TYPE, _SINK_VIDEO_CONFIG = 1, 4
_STREAM_TYPE_VIDEO = 3
# Video config fields
_VC_RESOLUTION, _VC_FPS, _VC_MARGIN_W, _VC_MARGIN_H, _VC_DENSITY = 1, 2, 3, 4, 5
# Input channel: touchscreen config, and its width/height
_IN_TOUCHSCREEN = 2
_TS_WIDTH, _TS_HEIGHT = 1, 2


@dataclass
class DisplayInfo:
    video_width: int | None = None
    video_height: int | None = None
    video_resolution_code: int | None = None  # raw enum, in case it's one we don't know
    fps: int | None = None
    margin_width: int = 0
    margin_height: int = 0
    density: int | None = None
    touch_width: int | None = None
    touch_height: int | None = None
    model: str = ""
    manufacturer: str = ""
    hu_model: str = ""
    sw_version: str = ""

    @property
    def visible_size(self) -> tuple[int, int] | None:
        """The area inside the margins -- what's guaranteed to be visible."""
        if self.video_width is None or self.video_height is None:
            return None
        return self.video_width - self.margin_width, self.video_height - self.margin_height

    def summary(self) -> str:
        if self.video_width is not None:
            video = f"video {self.video_width}x{self.video_height}"
        else:
            video = f"video resolution code {self.video_resolution_code} (unknown)"
        parts = [video + (f" @{self.fps}fps" if self.fps else "")]
        if self.visible_size:
            vw, vh = self.visible_size
            parts.append(f"margins {self.margin_width}x{self.margin_height} (visible area ~{vw}x{vh})")
        if self.density:
            parts.append(f"{self.density} dpi")
        if self.touch_width:
            parts.append(f"touchscreen {self.touch_width}x{self.touch_height}")
        who = " ".join(s for s in (self.manufacturer, self.hu_model, self.model) if s)
        if who:
            parts.append(f"'{who}'" + (f" sw {self.sw_version}" if self.sw_version else ""))
        return ", ".join(parts)


def _first_bytes(fields: dict, num: int) -> bytes | None:
    for f in fields.get(num, []):
        if f.wire_type == 2:
            return bytes(f.value)
    return None


def _text(fields: dict, num: int) -> str:
    value = _first_bytes(fields, num)
    return value.decode("utf-8", errors="replace") if value else ""


def parse_service_discovery_response(body: bytes) -> DisplayInfo:
    """Lenient: anything missing or unrecognized just stays None/empty."""
    top = decode_fields(body)
    info = DisplayInfo(
        model=_text(top, _SDR_MODEL), manufacturer=_text(top, _SDR_MANUFACTURER),
        hu_model=_text(top, _SDR_HU_MODEL), sw_version=_text(top, _SDR_SW_VERSION),
    )
    for channel in top.get(_SDR_CHANNEL, []):
        if channel.wire_type != 2:
            continue
        ch = decode_fields(bytes(channel.value))

        sink_bytes = _first_bytes(ch, _CH_MEDIA_SINK)
        if sink_bytes is not None:
            sink = decode_fields(sink_bytes)
            config_bytes = _first_bytes(sink, _SINK_VIDEO_CONFIG)
            if get_varint(sink, _SINK_TYPE) == _STREAM_TYPE_VIDEO and config_bytes is not None:
                vc = decode_fields(config_bytes)  # first config = the one AV setup's config_index 0 picks
                info.video_resolution_code = get_varint(vc, _VC_RESOLUTION)
                info.video_width, info.video_height = VIDEO_RESOLUTIONS.get(info.video_resolution_code, (None, None))
                info.fps = VIDEO_FPS.get(get_varint(vc, _VC_FPS))
                info.margin_width = get_varint(vc, _VC_MARGIN_W) or 0
                info.margin_height = get_varint(vc, _VC_MARGIN_H) or 0
                info.density = get_varint(vc, _VC_DENSITY)

        input_bytes = _first_bytes(ch, _CH_INPUT)
        if input_bytes is not None:
            touch_bytes = _first_bytes(decode_fields(input_bytes), _IN_TOUCHSCREEN)
            if touch_bytes is not None:
                ts = decode_fields(touch_bytes)
                info.touch_width, info.touch_height = get_varint(ts, _TS_WIDTH), get_varint(ts, _TS_HEIGHT)
    return info
