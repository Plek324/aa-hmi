from pathlib import Path

import pytest

from aa_hmi import messages
from aa_hmi.messages import MessageId, SecurityMode, parse_wifi_info_response

FIXTURES = Path(__file__).parent / "fixtures"


def _load_hex_fixture(name: str) -> bytes:
    lines = (FIXTURES / name).read_text().splitlines()
    hex_lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    return bytes.fromhex("".join(hex_lines))


def test_message_id_values_match_confirmed_enum():
    # Confirmed from aa-proxy-rs's public source -- see messages.py docstring.
    assert MessageId.WIFI_START_REQUEST == 1
    assert MessageId.WIFI_INFO_REQUEST == 2
    assert MessageId.WIFI_INFO_RESPONSE == 3
    assert MessageId.WIFI_VERSION_REQUEST == 4
    assert MessageId.WIFI_VERSION_RESPONSE == 5
    assert MessageId.WIFI_CONNECT_STATUS == 6
    assert MessageId.WIFI_START_RESPONSE == 7
    assert MessageId.WIFI_PING_REQUEST == 8
    assert MessageId.WIFI_PING_RESPONSE == 9
    assert MessageId.WIFI_SETUP_INFO == 11


def test_parse_real_wifi_info_response():
    payload = _load_hex_fixture("wifi_info_response_tf811.hex")
    info = parse_wifi_info_response(payload)
    assert info.ssid == "TF811-1c64201a"
    assert info.key == "12345678"
    assert info.bssid == "68:8f:c9:b3:24:37"
    assert info.security_mode_raw == 5
    assert info.security_mode_raw not in [m.value for m in SecurityMode]
    assert info.access_point_type_raw is None
    assert info.is_open is False


def test_parse_wifi_info_response_missing_ssid_or_key_raises():
    from aa_hmi import protocol

    # only bssid (field 3), no ssid/key
    payload = protocol.encode_string_field(3, "aa:bb:cc:dd:ee:ff")
    with pytest.raises(ValueError):
        parse_wifi_info_response(payload)


def test_open_network_detection_is_key_driven_not_enum_driven():
    from aa_hmi import protocol
    from aa_hmi.messages import parse_wifi_info_response

    payload = (protocol.encode_string_field(1, "OpenNet")
               + protocol.encode_string_field(2, ""))  # empty key
    info = parse_wifi_info_response(payload)
    assert info.is_open is True


def test_ground_truth_is_confirmed_and_matches_the_real_capture():
    """Confirmed 2026-09-18 against a real TF811BT display -- see
    messages.py's module docstring and
    tests/fixtures/ground_truth_probe_session.txt for the raw capture.
    Guardrail: if this ever needs to change (e.g. a different head unit
    needs a non-empty WifiInfoRequest), update it alongside a real
    captured fixture, not by just editing this assertion."""
    assert messages.GROUND_TRUTH_CONFIRMED is True
    assert messages.WIFI_INFO_REQUEST_PAYLOAD == b""
