"""display_info.py against a real ServiceDiscoveryResponse, captured from
a Podofo/TF811BT display (rebuilt byte-for-byte from `aa-hmi serve -v`'s
field dump; 962 bytes, same as the logged length)."""
from pathlib import Path

from aa_hmi.display_info import DisplayInfo, parse_service_discovery_response
from aa_hmi.protocol import encode_varint_field

FIXTURE = Path(__file__).parent / "fixtures" / "service_discovery_response_podofo.hex"


def _podofo():
    return parse_service_discovery_response(bytes.fromhex(FIXTURE.read_text().strip()))


def test_podofo_video_config():
    info = _podofo()
    assert (info.video_width, info.video_height, info.fps) == (800, 480, 30)
    assert (info.margin_width, info.margin_height, info.density) == (18, 40, 140)
    assert info.visible_size == (782, 440)


def test_podofo_touchscreen_and_identity():
    info = _podofo()
    assert (info.touch_width, info.touch_height) == (800, 480)
    assert (info.manufacturer, info.hu_model, info.model, info.sw_version) == \
        ("ZJ", "zlink5", "Desktop Head Unit", "1.0.1")


def test_summary_mentions_the_essentials():
    s = _podofo().summary()
    assert "800x480" in s and "visible area ~782x440" in s and "touchscreen 800x480" in s


def test_unknown_resolution_code_is_kept_raw():
    video_config = encode_varint_field(1, 9)
    sink = encode_varint_field(1, 3) + b"\x22" + bytes([len(video_config)]) + video_config
    channel = encode_varint_field(1, 3) + b"\x1a" + bytes([len(sink)]) + sink
    body = b"\x0a" + bytes([len(channel)]) + channel
    info = parse_service_discovery_response(body)
    assert info.video_resolution_code == 9 and info.video_width is None and info.visible_size is None
    assert "unknown" in info.summary()


def test_garbage_does_not_raise():
    assert isinstance(parse_service_discovery_response(b"\xff\xff\xff"), DisplayInfo)
