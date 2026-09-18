#!/usr/bin/env python3
"""aa-hmi -- scan for a Bluetooth AA-Wireless head unit, pair with it, pull
its WiFi credentials over the RFCOMM bootstrap handshake, join that WiFi
network, and cache the device so next time is instant.

Scope: Bluetooth scan -> pair -> WiFi-credential bootstrap -> WiFi connect
-> credential caching. This is NOT an Android Auto video client -- see
README.md. The sibling project aa_pi2display consumes credentials/a live
connection obtained this way to stream video over the resulting WiFi link.

Usage:
    aa-hmi run                         # interactive: scan, pick, pair, connect
    aa-hmi run --rescan                # force a fresh scan even if cached
    aa-hmi run --device AA:BB:CC:DD:EE:FF --non-interactive
    aa-hmi run --no-wifi-connect --json   # just print credentials
    aa-hmi list
    aa-hmi forget "TF811BT_xxxxxxxx"
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from . import cache, coexistence, wifi
from .bootstrap import get_wifi_info, try_get_wifi_info
from .discovery import BluetoothCtl, BtDevice
from .errors import AaHmiError, GroundTruthNotConfirmedError, NoChannelFoundError, NoDeviceSelectedError
from .log import log
from .messages import WifiInfo
from .retry import RetryExhaustedError, is_retryable_oserror, retry_with_backoff
from .rfcomm import connect_rfcomm, find_channel


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aa-hmi", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="scan/pair/bootstrap/connect (default)")
    run.add_argument("--rescan", action="store_true", help="force a fresh BT scan + picker even if a device is cached")
    run.add_argument("--device", metavar="MAC", help="target a specific device (must be cached, or combine with --channel)")
    run.add_argument("--channel", type=int, metavar="N", help="force an RFCOMM channel, skip discovery")
    run.add_argument("--non-interactive", action="store_true", help="never prompt; fail if no usable cached device")
    run.add_argument("--no-wifi-connect", action="store_true", help="stop after the handshake; print/return credentials only")
    run.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    run.add_argument("--radio-coexistence-workaround", action="store_true",
                      help="disconnect WiFi before the Bluetooth step, for combo-chip hardware that can't use both radios at once (e.g. Raspberry Pi's onboard BCM4345C0)")
    run.add_argument("--wifi-iface", metavar="IFACE", help="WiFi interface to use (default: autodetect)")
    run.add_argument("--bt-timeout", type=float, default=90.0, metavar="SECONDS",
                      help="total time budget for Bluetooth-stage retries (default: 90)")
    run.add_argument("--scan-duration", type=float, default=10.0, metavar="SECONDS", help="BT scan duration (default: 10)")
    run.add_argument("--config-dir", metavar="PATH", help="override the cache directory (default: $XDG_CONFIG_HOME/aa-hmi)")
    run.add_argument("-v", "--verbose", action="store_true")

    lst = sub.add_parser("list", help="show cached devices")
    lst.add_argument("--config-dir", metavar="PATH")

    forget = sub.add_parser("forget", help="remove a cached device")
    forget.add_argument("mac_or_name", nargs="?")
    forget.add_argument("--all", action="store_true", help="forget every cached device")
    forget.add_argument("--config-dir", metavar="PATH")

    return p


def _cache_path(args) -> Path:
    return Path(args.config_dir) if args.config_dir else cache.default_cache_path()


def _pick_device_interactively(devices: list[BtDevice]) -> BtDevice | None:
    if not devices:
        print("No Bluetooth devices found.", file=sys.stderr)
        return None
    print("\nBluetooth devices found:", file=sys.stderr)
    for i, d in enumerate(devices, 1):
        print(f"  {i}. {d.name}  [{d.mac}]", file=sys.stderr)
    while True:
        choice = input(f"Select a device [1-{len(devices)}] (or blank to cancel): ").strip()
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(devices):
            return devices[int(choice) - 1]
        print("Invalid choice.", file=sys.stderr)


def _resolve_device(args, bt: BluetoothCtl, cache_path: Path) -> tuple[str, cache.DeviceRecord | None]:
    """Returns (mac, cached_record_or_None)."""
    if args.device:
        return args.device, cache.get_device(cache_path, args.device)

    if not args.rescan:
        devices = cache.list_devices(cache_path)
        if len(devices) == 1:
            return devices[0].mac, devices[0]
        if len(devices) > 1 and args.non_interactive:
            raise NoDeviceSelectedError(
                f"{len(devices)} cached devices and --non-interactive given -- "
                f"use --device MAC to pick one."
            )
        if len(devices) > 1:
            print("\nCached devices:", file=sys.stderr)
            for i, d in enumerate(devices, 1):
                print(f"  {i}. {d.name}  [{d.mac}]", file=sys.stderr)
            choice = input(f"Select a cached device [1-{len(devices)}] (blank to rescan): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(devices):
                rec = devices[int(choice) - 1]
                return rec.mac, rec

    if args.non_interactive:
        raise NoDeviceSelectedError("no cached device and --non-interactive given -- run once interactively first")

    bt.power_on()
    found = bt.scan(duration_s=args.scan_duration)
    picked = _pick_device_interactively(found)
    if picked is None:
        raise NoDeviceSelectedError("no device selected")
    return picked.mac, cache.get_device(cache_path, picked.mac)


def _bootstrap_wifi_info(mac: str, channel: int | None, cached: cache.DeviceRecord | None,
                          bt_timeout: float) -> tuple[WifiInfo, int, str]:
    """Returns (WifiInfo, channel_used, discovery_method)."""

    def attempt():
        if channel is not None:
            sock = connect_rfcomm(mac, channel)
            try:
                return get_wifi_info(sock), channel, "forced"
            finally:
                sock.close()
        if cached and cached.rfcomm_channel is not None:
            try:
                sock = connect_rfcomm(mac, cached.rfcomm_channel)
                try:
                    return get_wifi_info(sock), cached.rfcomm_channel, cached.discovery_method or "cached"
                finally:
                    sock.close()
            except (OSError, AaHmiError) as exc:
                log(f"  cached channel {cached.rfcomm_channel} failed ({exc}), rediscovering...")
        result = find_channel(mac, try_get_wifi_info)
        try:
            info = get_wifi_info(result.sock)
        finally:
            result.sock.close()
        return info, result.channel, result.discovery_method

    try:
        return retry_with_backoff(
            attempt, max_attempts=20, base_delay=2.0, max_delay=15.0,
            time_budget=bt_timeout, retryable=_bt_stage_retryable,
        )
    except RetryExhaustedError as exc:
        from .errors import HINT_FLAKY_CLASSIC_BT, HINT_SINGLE_CONNECTION_SLOT
        print(f"\n{HINT_SINGLE_CONNECTION_SLOT}\n\n{HINT_FLAKY_CLASSIC_BT}", file=sys.stderr)
        raise SystemExit(1) from exc


def _bt_stage_retryable(exc: BaseException) -> bool:
    return is_retryable_oserror(exc) or isinstance(exc, (NoChannelFoundError, socket.timeout))


def cmd_run(args) -> int:
    """Thin wrapper: translates every expected failure mode into a clean
    message + exit code, so nothing this tool can anticipate (a missing
    bluetoothctl/nmcli, a pairing failure, an unconfirmed-ground-truth
    guard, Ctrl+C during an interactive prompt) ever surfaces as a raw
    Python traceback to the end user."""
    try:
        return _cmd_run_inner(args)
    except GroundTruthNotConfirmedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except AaHmiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SystemExit:
        return 1
    except KeyboardInterrupt:
        print("\ncancelled.", file=sys.stderr)
        return 130


def _cmd_run_inner(args) -> int:
    cache_path = _cache_path(args)
    bt = BluetoothCtl(timeout=max(10.0, args.bt_timeout / 6))

    mac, cached = _resolve_device(args, bt, cache_path)

    bt.power_on()
    if not bt.pair(mac):
        print(f"error: failed to pair with {mac}", file=sys.stderr)
        return 1

    with coexistence.wifi_disconnected_for_bluetooth(
        args.wifi_iface, enabled=args.radio_coexistence_workaround
    ):
        info, channel_used, method = _bootstrap_wifi_info(mac, args.channel, cached, args.bt_timeout)

        cache.upsert_device(
            cache_path, mac,
            rfcomm_channel=channel_used, discovery_method=method,
            last_ssid=info.ssid, last_key=info.key, last_bssid=info.bssid,
            last_security_mode_raw=info.security_mode_raw,
            last_seen=cache.now_iso(), last_success=cache.now_iso(),
        )

        ip = None
        if not args.no_wifi_connect:
            ip = wifi.connect(info.ssid, info.key, iface=args.wifi_iface)

    result = {
        "mac": mac, "ssid": info.ssid, "key": info.key, "bssid": info.bssid,
        "security_mode_raw": info.security_mode_raw, "rfcomm_channel": channel_used,
        "discovery_method": method, "wifi_connected": ip is not None, "ip": ip,
    }
    if args.json:
        print(json.dumps(result))
    else:
        print(f"\nSSID: {info.ssid}")
        print(f"Password: {info.key}")
        if info.bssid:
            print(f"BSSID: {info.bssid}")
        if ip:
            print(f"Connected -- IP address: {ip}")
    return 0


def cmd_list(args) -> int:
    devices = cache.list_devices(_cache_path(args))
    if not devices:
        print("No cached devices.")
        return 0
    for d in devices:
        print(f"{d.mac}  {d.name or '(unnamed)'}  channel={d.rfcomm_channel} "
              f"ssid={d.last_ssid!r} last_success={d.last_success}")
    return 0


def cmd_forget(args) -> int:
    path = _cache_path(args)
    if args.all:
        cache.forget_all(path)
        print("Forgot all cached devices.")
        return 0
    if not args.mac_or_name:
        print("error: give a MAC/name, or use --all", file=sys.stderr)
        return 1
    if cache.forget(path, args.mac_or_name):
        print(f"Forgot {args.mac_or_name}.")
        return 0
    print(f"No cached device matching {args.mac_or_name!r}.", file=sys.stderr)
    return 1


_SUBCOMMANDS = ("run", "list", "forget")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # Default to the `run` subcommand when none is given, e.g. `aa-hmi -v`
    # or bare `aa-hmi` -- but not for `-h`/`--help`, which argparse should
    # handle at the top level.
    if (not argv or argv[0] not in _SUBCOMMANDS) and not (argv and argv[0] in ("-h", "--help")):
        argv = ["run", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    if command == "run":
        return cmd_run(args)
    if command == "list":
        return cmd_list(args)
    if command == "forget":
        return cmd_forget(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
