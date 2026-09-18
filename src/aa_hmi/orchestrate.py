"""orchestrate.py -- device selection + WiFi-credential bootstrap,
shared by both `aa-hmi run` (cli.py) and `aa-hmi serve` (daemon.py).

Extracted from cli.py (where this used to live as private
_resolve_device/_bootstrap_wifi_info functions) so daemon.py can reuse it
without importing cli.py -- cli.py stays a thin argument-parsing/dispatch
layer, this module is the actual reusable logic, and it takes an explicit
DeviceSelectionArgs dataclass instead of an argparse Namespace so it has
no argparse dependency at all.
"""
from __future__ import annotations

import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import cache
from .bootstrap import confirm_wifi_connected, get_wifi_info, try_get_wifi_info
from .discovery import BluetoothCtl, BtDevice
from .errors import AaHmiError, HINT_FLAKY_CLASSIC_BT, HINT_SINGLE_CONNECTION_SLOT, NoChannelFoundError, NoDeviceSelectedError
from .log import log
from .messages import WifiInfo
from .retry import RetryExhaustedError, is_retryable_oserror, retry_with_backoff
from .rfcomm import connect_rfcomm, find_channel


@dataclass
class DeviceSelectionArgs:
    device: str | None = None
    rescan: bool = False
    non_interactive: bool = False
    scan_duration: float = 10.0


def pick_device_interactively(devices: list[BtDevice]) -> BtDevice | None:
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


def resolve_device(args: DeviceSelectionArgs, bt: BluetoothCtl, cache_path: Path) -> tuple[str, cache.DeviceRecord | None]:
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
    picked = pick_device_interactively(found)
    if picked is None:
        raise NoDeviceSelectedError("no device selected")
    return picked.mac, cache.get_device(cache_path, picked.mac)


def bootstrap_wifi_info(mac: str, channel: int | None, cached: cache.DeviceRecord | None,
                         bt_timeout: float, *, confirm_connected: bool = False,
                         cancel_event: threading.Event | None = None,
                         ) -> tuple[WifiInfo, int, str, socket.socket | None]:
    """Returns (WifiInfo, channel_used, discovery_method, rfcomm_sock).

    confirm_connected=True additionally sends WifiStartResponse +
    WifiConnectStatus over the RFCOMM socket right after a successful
    WifiInfoResponse, and -- this is the part that was missing before a
    real integration bug was found live -- KEEPS THAT SOCKET OPEN,
    returning it as `rfcomm_sock` instead of closing it. Confirmed live
    (2026-09-18): the display's TCP video port only accepts a connection
    while this RFCOMM/Bluetooth link is still open -- sending the confirm
    messages alone, on an already-closed socket, was NOT enough; the real
    `aa-proxy-rs` probe this was ported from keeps its own RFCOMM
    connection alive for up to 45s specifically for this reason ("keeping
    RFCOMM open until HU closes or 30s idle timeout"). Callers that pass
    confirm_connected=True (daemon.py) MUST close the returned socket
    themselves once the TCP/TLS video session is safely established --
    see bootstrap.confirm_wifi_connected's docstring and
    docs/video-protocol-notes.md.

    confirm_connected=False (the default, used by `aa-hmi run`'s
    credentials/WiFi-connect-only scope) closes the socket immediately as
    before and always returns rfcomm_sock=None.

    cancel_event, if given, is passed through to retry_with_backoff --
    lets a caller (daemon.py, its own shutdown stop_event) make this
    return promptly (raising retry.RetryCancelledError) instead of
    grinding through the full retry/backoff budget when asked to stop.
    See retry_with_backoff's own docstring for what this does and does
    NOT cover (an attempt already in flight still can't be interrupted).
    """

    def _get_info(sock: socket.socket) -> WifiInfo:
        info = get_wifi_info(sock)
        if confirm_connected:
            confirm_wifi_connected(sock)
        return info

    def attempt():
        if channel is not None:
            sock = connect_rfcomm(mac, channel)
            return _finish(sock, sock, channel, "forced")
        if cached and cached.rfcomm_channel is not None:
            try:
                sock = connect_rfcomm(mac, cached.rfcomm_channel)
                return _finish(sock, sock, cached.rfcomm_channel, cached.discovery_method or "cached")
            except (OSError, AaHmiError) as exc:
                log(f"  cached channel {cached.rfcomm_channel} failed ({exc}), rediscovering...")
        result = find_channel(mac, try_get_wifi_info, cancel_event=cancel_event)
        return _finish(result.sock, result.sock, result.channel, result.discovery_method)

    def _finish(sock: socket.socket, sock_for_return: socket.socket, channel_used: int, method: str):
        try:
            info = _get_info(sock)
        except Exception:
            sock.close()
            raise
        if not confirm_connected:
            sock.close()
            return info, channel_used, method, None
        return info, channel_used, method, sock_for_return

    try:
        return retry_with_backoff(
            attempt, max_attempts=20, base_delay=2.0, max_delay=15.0,
            time_budget=bt_timeout, retryable=bt_stage_retryable,
            cancel_event=cancel_event,
        )
    except RetryExhaustedError as exc:
        print(f"\n{HINT_SINGLE_CONNECTION_SLOT}\n\n{HINT_FLAKY_CLASSIC_BT}", file=sys.stderr)
        raise SystemExit(1) from exc


def bt_stage_retryable(exc: BaseException) -> bool:
    return is_retryable_oserror(exc) or isinstance(exc, (NoChannelFoundError, socket.timeout))
