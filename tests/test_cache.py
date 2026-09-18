import stat
import sys

import pytest

from aa_hmi import cache


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "aa-hmi" / "devices.json"


def test_load_missing_file_returns_empty_cache(cache_path):
    c = cache.load(cache_path)
    assert c.devices == {}
    assert c.version == cache.CACHE_SCHEMA_VERSION


def test_upsert_and_get_roundtrip(cache_path):
    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", name="TF811BT", rfcomm_channel=4,
                         last_ssid="TF811-1c64201a", last_key="12345678")
    rec = cache.get_device(cache_path, "AA:BB:CC:DD:EE:FF")
    assert rec is not None
    assert rec.name == "TF811BT"
    assert rec.rfcomm_channel == 4
    assert rec.last_ssid == "TF811-1c64201a"


def test_upsert_updates_existing_record(cache_path):
    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", rfcomm_channel=4)
    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", rfcomm_channel=7)
    rec = cache.get_device(cache_path, "AA:BB:CC:DD:EE:FF")
    assert rec.rfcomm_channel == 7


def test_list_devices(cache_path):
    cache.upsert_device(cache_path, "AA:AA:AA:AA:AA:AA", name="one")
    cache.upsert_device(cache_path, "BB:BB:BB:BB:BB:BB", name="two")
    names = sorted(d.name for d in cache.list_devices(cache_path))
    assert names == ["one", "two"]


def test_forget_by_mac(cache_path):
    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", name="TF811BT")
    assert cache.forget(cache_path, "AA:BB:CC:DD:EE:FF") is True
    assert cache.get_device(cache_path, "AA:BB:CC:DD:EE:FF") is None


def test_forget_by_name(cache_path):
    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", name="TF811BT")
    assert cache.forget(cache_path, "TF811BT") is True
    assert cache.list_devices(cache_path) == []


def test_forget_unknown_returns_false(cache_path):
    assert cache.forget(cache_path, "nope") is False


def test_forget_all(cache_path):
    cache.upsert_device(cache_path, "AA:AA:AA:AA:AA:AA", name="one")
    cache.upsert_device(cache_path, "BB:BB:BB:BB:BB:BB", name="two")
    cache.forget_all(cache_path)
    assert cache.list_devices(cache_path) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions don't apply on Windows")
def test_save_sets_0600_permissions(cache_path):
    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", last_key="12345678")
    mode = stat.S_IMODE(cache_path.stat().st_mode)
    assert mode == 0o600


def test_corrupt_cache_file_treated_as_empty(cache_path):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not valid json")
    c = cache.load(cache_path)
    assert c.devices == {}


def test_default_cache_path_respects_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgcfg"))
    assert cache.default_cache_path() == tmp_path / "xdgcfg" / "aa-hmi" / "devices.json"
