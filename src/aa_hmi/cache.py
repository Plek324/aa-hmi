"""cache.py -- JSON device/credential cache, keyed by Bluetooth MAC.

Stores enough per-device state that a later run can skip straight to
"connect to known MAC on known RFCOMM channel, re-run the handshake, join
WiFi" -- no scan, no interactive picker, no channel brute force.

Every function here takes an explicit path rather than resolving one
itself, so tests never touch a real home directory (see
default_cache_path() for the one place the real default lives).

The cache file holds a plaintext WiFi password (whatever the head unit's
WifiInfoResponse handed back) -- save() always chmods it 0600. This is
called out explicitly in the README's security section; it is not meant to
be a secrets vault, just a convenience cache for one's own local machine.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import pwd  # POSIX only; used for the sudo-aware home lookup below
except ImportError:  # pragma: no cover -- exercised via monkeypatching on non-POSIX
    pwd = None  # type: ignore[assignment]

CACHE_SCHEMA_VERSION = 1


def default_cache_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else _real_user_home() / ".config"
    return base / "aa-hmi" / "devices.json"


def _real_user_home() -> Path:
    """Path.home() resolves to root's home when running under sudo (sudo
    resets $HOME to the target user's by default) -- confirmed live
    (2026-09-18): `sudo aa-hmi run` (the documented workaround for
    nmcli's polkit "Not authorized to control networking" -- see
    README's Troubleshooting section) wrote the cache to /root/.config
    instead of the invoking user's home, silently defeating the whole
    point of caching for that person's later non-sudo runs. Falls back
    to the real invoking user's home (via $SUDO_USER + the passwd
    database) when actually running as root through sudo; Path.home()
    otherwise."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and pwd is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class DeviceRecord:
    mac: str
    name: str | None = None
    rfcomm_channel: int | None = None
    discovery_method: str | None = None  # "sdp" or "bruteforce"
    last_ssid: str | None = None
    last_key: str | None = None
    last_bssid: str | None = None
    last_security_mode_raw: int | None = None
    last_seen: str | None = None
    last_success: str | None = None


@dataclass
class Cache:
    version: int = CACHE_SCHEMA_VERSION
    devices: dict[str, DeviceRecord] = field(default_factory=dict)


def load(path: Path) -> Cache:
    if not path.exists():
        return Cache()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt cache shouldn't crash the tool -- treat it as empty and
        # let the next save() overwrite it.
        return Cache()
    devices = {
        mac: DeviceRecord(mac=mac, **{k: v for k, v in rec.items() if k != "mac"})
        for mac, rec in raw.get("devices", {}).items()
    }
    return Cache(version=raw.get("version", CACHE_SCHEMA_VERSION), devices=devices)


def save(path: Path, cache: Cache) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": cache.version,
        "devices": {mac: {k: v for k, v in asdict(rec).items() if k != "mac"}
                    for mac, rec in cache.devices.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- holds a plaintext WiFi password
    except OSError:
        pass  # best-effort on platforms/filesystems that don't support it (e.g. some Windows dev setups)
    _maybe_chown_to_real_user(path)


def _maybe_chown_to_real_user(path: Path) -> None:
    """When running as root via sudo, _real_user_home() already points
    the cache at the real invoking user's home -- but the file/directory
    this process actually creates are still root-owned, which blocks
    that same person's later non-sudo runs from reading OR updating it
    (confirmed live, 2026-09-18: `sudo aa-hmi run` left a 0600 root-owned
    file that a plain `aa-hmi list` as the normal user couldn't see).
    Chowns the cache file and its immediate parent directory back to
    $SUDO_UID:$SUDO_GID in that case -- just the immediate parent, not
    the whole path, since the common case is that $HOME/.config already
    exists with correct ownership and only aa-hmi's own subdirectory
    under it is new."""
    sudo_uid, sudo_gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (sudo_uid and sudo_gid and hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    try:
        uid, gid = int(sudo_uid), int(sudo_gid)
        os.chown(path, uid, gid)
        os.chown(path.parent, uid, gid)
    except (OSError, ValueError):
        pass  # best-effort -- don't fail a save() over cosmetic ownership


def upsert_device(path: Path, mac: str, **fields) -> DeviceRecord:
    cache = load(path)
    rec = cache.devices.get(mac, DeviceRecord(mac=mac))
    for k, v in fields.items():
        setattr(rec, k, v)
    cache.devices[mac] = rec
    save(path, cache)
    return rec


def get_device(path: Path, mac: str) -> DeviceRecord | None:
    return load(path).devices.get(mac)


def find_device_by_name(path: Path, name: str) -> DeviceRecord | None:
    for rec in load(path).devices.values():
        if rec.name == name:
            return rec
    return None


def list_devices(path: Path) -> list[DeviceRecord]:
    return list(load(path).devices.values())


def forget(path: Path, mac_or_name: str) -> bool:
    cache = load(path)
    if mac_or_name in cache.devices:
        del cache.devices[mac_or_name]
        save(path, cache)
        return True
    for mac, rec in list(cache.devices.items()):
        if rec.name == mac_or_name:
            del cache.devices[mac]
            save(path, cache)
            return True
    return False


def forget_all(path: Path) -> None:
    save(path, Cache())
