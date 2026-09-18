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


def test_save_chowns_file_and_dir_to_sudo_user_when_run_as_root(monkeypatch, cache_path):
    """Regression test for a real failure hit during live testing
    (2026-09-18): `sudo aa-hmi run` left the cache file AND its directory
    owned by root, so a later plain `aa-hmi list`/`run` as the real user
    couldn't read or update it -- even after default_cache_path() was
    already fixed to point at that user's home. chown is mocked since
    this needs to run as a real test on any platform/user, not actually
    require root."""
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setattr(cache.os, "geteuid", lambda: 0, raising=False)
    chowned = []
    monkeypatch.setattr(cache.os, "chown", lambda p, uid, gid, **kw: chowned.append((str(p), uid, gid)), raising=False)

    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", last_key="12345678")

    assert (str(cache_path), 1000, 1000) in chowned
    assert (str(cache_path.parent), 1000, 1000) in chowned


def test_save_does_not_chown_when_not_running_as_root(monkeypatch, cache_path):
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setattr(cache.os, "geteuid", lambda: 1000, raising=False)  # not root
    chown_calls = []
    monkeypatch.setattr(cache.os, "chown", lambda *a, **kw: chown_calls.append(a), raising=False)

    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", last_key="12345678")

    assert chown_calls == []


def test_save_does_not_chown_when_sudo_env_vars_absent(monkeypatch, cache_path):
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)
    monkeypatch.setattr(cache.os, "geteuid", lambda: 0, raising=False)  # root, but not via sudo
    chown_calls = []
    monkeypatch.setattr(cache.os, "chown", lambda *a, **kw: chown_calls.append(a), raising=False)

    cache.upsert_device(cache_path, "AA:BB:CC:DD:EE:FF", last_key="12345678")

    assert chown_calls == []


def test_corrupt_cache_file_treated_as_empty(cache_path):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not valid json")
    c = cache.load(cache_path)
    assert c.devices == {}


def test_default_cache_path_respects_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgcfg"))
    assert cache.default_cache_path() == tmp_path / "xdgcfg" / "aa-hmi" / "devices.json"


def test_real_user_home_resolves_sudo_users_home_not_roots(monkeypatch, tmp_path):
    """Regression test for a real failure hit during live testing
    (2026-09-18): running `sudo aa-hmi run` (the documented workaround
    for nmcli's polkit error) silently wrote the cache under /root
    instead of the invoking user's home. Simulates the sudo environment
    (SUDO_USER set, effective uid 0, a fake pwd database) without
    depending on the real POSIX pwd module, so this is testable on any
    platform."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SUDO_USER", "peter")
    monkeypatch.setattr(cache.os, "geteuid", lambda: 0, raising=False)

    real_home = tmp_path / "home" / "peter"

    class FakePwEntry:
        pw_dir = str(real_home)

    class FakePwd:
        @staticmethod
        def getpwnam(name):
            assert name == "peter"
            return FakePwEntry()

    monkeypatch.setattr(cache, "pwd", FakePwd())
    assert cache._real_user_home() == real_home
    assert cache.default_cache_path() == real_home / ".config" / "aa-hmi" / "devices.json"


def test_real_user_home_falls_back_to_path_home_when_not_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SUDO_USER", "peter")  # set, but euid isn't 0 -- shouldn't matter
    monkeypatch.setattr(cache.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(cache.Path, "home", classmethod(lambda cls: tmp_path / "not-root"))
    assert cache._real_user_home() == tmp_path / "not-root"


def test_real_user_home_falls_back_to_path_home_when_sudo_user_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(cache.Path, "home", classmethod(lambda cls: tmp_path / "plain"))
    assert cache._real_user_home() == tmp_path / "plain"
