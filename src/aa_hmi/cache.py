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

CACHE_SCHEMA_VERSION = 1


def default_cache_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "aa-hmi" / "devices.json"


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
