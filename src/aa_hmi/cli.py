#!/usr/bin/env python3
"""aa-hmi -- the generic display-server layer for an AA-Wireless-style
Bluetooth head unit: scans, pairs, pulls WiFi credentials, joins that
WiFi network, holds the TCP/TLS video session with the display, and
relays frames/touch between it and any separate content-producing
program over a small local socket protocol.

Two ways to use it:
- `aa-hmi run`: just the Bluetooth+WiFi bootstrap (credentials/connect
  only) -- a building block, useful on its own.
- `aa-hmi serve`: the full daemon -- bootstrap, then hold the display
  session open and bridge it to a local socket other programs connect to.
  See docs/ipc-protocol.md and examples/ for the client side of that.

Usage:
    aa-hmi run                         # interactive: scan, pick, pair, connect
    aa-hmi run --rescan                # force a fresh scan even if cached
    aa-hmi run --device AA:BB:CC:DD:EE:FF --non-interactive
    aa-hmi run --no-wifi-connect --json   # just print credentials
    aa-hmi serve                       # bootstrap + hold the display session + serve IPC
    aa-hmi list
    aa-hmi forget "TF811BT_xxxxxxxx"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import cache, coexistence, orchestrate, wifi
from .discovery import BluetoothCtl
from .errors import (
    AaHmiError, GroundTruthNotConfirmedError, HINT_NMCLI_NOT_AUTHORIZED, NmcliError,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aa-hmi", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="scan/pair/bootstrap/connect (default)")
    _add_bootstrap_args(run)
    run.add_argument("--no-wifi-connect", action="store_true", help="stop after the handshake; print/return credentials only")
    run.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    run.add_argument("-v", "--verbose", action="store_true")

    serve = sub.add_parser("serve", help="run the full display-server daemon")
    _add_bootstrap_args(serve)
    serve.set_defaults(non_interactive=True)  # an unattended daemon must never block on input()
    # On by default for `serve` specifically (off for `run`): serve's own
    # reconnect loop redoes the full Bluetooth bootstrap on every drop, so
    # WiFi/BT radio contention on combo-chip hardware would otherwise
    # recur on every single reconnect, not just a one-off. Also the more
    # robust half of the fix for a real bug found live (2026-09-22):
    # leftover WiFi state (Ctrl+C not cleaning up, a crash, a stale
    # connection from any other cause) blocking the next start's
    # Bluetooth step -- this makes every bootstrap attempt defensively
    # clear WiFi first regardless of how we got into that state, not just
    # when the *previous* run happened to shut down gracefully. Use
    # --no-radio-coexistence-workaround to opt back out (e.g. non-combo-chip
    # hardware where it's pure overhead).
    serve.set_defaults(radio_coexistence_workaround=True)
    serve.add_argument("--socket-path", metavar="PATH", help="IPC socket location (default: $XDG_RUNTIME_DIR/aa-hmi/video.sock)")
    serve.add_argument("--display-ip", default="192.168.10.1", help="display's IP on its own WiFi AP (default: 192.168.10.1)")
    serve.add_argument("--cert", metavar="PATH", help="TLS cert path (default: auto-generated under --config-dir)")
    serve.add_argument("--key", metavar="PATH", help="TLS key path (default: auto-generated under --config-dir)")
    serve.add_argument("--persistent-session", action="store_true",
                        help="EXPERIMENTAL, unverified (see docs/video-protocol-notes.md): assume the TCP/TLS "
                             "session survives indefinitely without re-arming Bluetooth; still re-arms on any "
                             "actual drop, but logs/counts drops for soak-test analysis instead of treating "
                             "re-arming as simply routine")
    serve.add_argument("--reconnect-max-attempts", type=int, default=None,
                        help="give up after this many consecutive reconnect failures (default: retry forever)")
    serve.add_argument("--liveness-timeout", type=float, default=60.0, metavar="SECONDS",
                        help="reconnect if the display sends nothing at all (not even an ACK) for this long, "
                             "even though the connection otherwise looks fine -- the display's own video decoder "
                             "has been observed to wedge silently under sustained use (see "
                             "docs/video-protocol-notes.md); 0 disables this check (default: 60)")
    serve.add_argument("-v", "--verbose", action="store_true")

    lst = sub.add_parser("list", help="show cached devices")
    lst.add_argument("--config-dir", metavar="PATH")

    forget = sub.add_parser("forget", help="remove a cached device")
    forget.add_argument("mac_or_name", nargs="?")
    forget.add_argument("--all", action="store_true", help="forget every cached device")
    forget.add_argument("--config-dir", metavar="PATH")

    return p


def _add_bootstrap_args(sp: argparse.ArgumentParser) -> None:
    """Flags shared by `run` and `serve` -- both go through the same
    orchestrate.resolve_device/bootstrap_wifi_info path."""
    sp.add_argument("--rescan", action="store_true", help="force a fresh BT scan + picker even if a device is cached")
    sp.add_argument("--device", metavar="MAC", help="target a specific device (must be cached, or combine with --channel)")
    sp.add_argument("--channel", type=int, metavar="N", help="force an RFCOMM channel, skip discovery")
    sp.add_argument("--non-interactive", action="store_true", help="never prompt; fail if no usable cached device")
    sp.add_argument("--radio-coexistence-workaround", action=argparse.BooleanOptionalAction, default=False,
                     help="disconnect WiFi before the Bluetooth step, for combo-chip hardware that can't use both radios at once (e.g. Raspberry Pi's onboard BCM4345C0). "
                          "Default off for `run`, on for `serve` (--no-radio-coexistence-workaround to disable there)")
    sp.add_argument("--wifi-iface", metavar="IFACE", help="WiFi interface to use (default: autodetect)")
    sp.add_argument("--bt-timeout", type=float, default=90.0, metavar="SECONDS",
                     help="total time budget for Bluetooth-stage retries (default: 90)")
    sp.add_argument("--scan-duration", type=float, default=10.0, metavar="SECONDS", help="BT scan duration (default: 10)")
    sp.add_argument("--config-dir", metavar="PATH", help="override the cache directory (default: $XDG_CONFIG_HOME/aa-hmi)")


def _cache_path(args) -> Path:
    return Path(args.config_dir) if args.config_dir else cache.default_cache_path()


def _selection_args(args) -> orchestrate.DeviceSelectionArgs:
    return orchestrate.DeviceSelectionArgs(
        device=args.device, rescan=args.rescan,
        non_interactive=args.non_interactive, scan_duration=args.scan_duration,
    )


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
    except NmcliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "not authorized" in str(exc).lower():
            print(f"\n{HINT_NMCLI_NOT_AUTHORIZED}", file=sys.stderr)
        return 1
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

    mac, cached = orchestrate.resolve_device(_selection_args(args), bt, cache_path)

    bt.power_on()
    if not bt.pair(mac):
        print(f"error: failed to pair with {mac}", file=sys.stderr)
        return 1

    with coexistence.wifi_disconnected_for_bluetooth(
        args.wifi_iface, enabled=args.radio_coexistence_workaround
    ):
        info, channel_used, method, _rfcomm_sock = orchestrate.bootstrap_wifi_info(
            mac, args.channel, cached, args.bt_timeout,
        )  # _rfcomm_sock is always None here -- confirm_connected defaults to False for `run`

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


def cmd_serve(args) -> int:
    """Thin wrapper matching cmd_run's shape -- daemon.py owns the actual
    orchestration/reconnect logic, cli.py just parses args and translates
    exceptions to exit codes the same way cmd_run does."""
    from . import daemon  # local import: keeps `aa-hmi run`/list/forget free of video/ipc imports
    try:
        return daemon.run_daemon(args)
    except GroundTruthNotConfirmedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except NmcliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "not authorized" in str(exc).lower():
            print(f"\n{HINT_NMCLI_NOT_AUTHORIZED}", file=sys.stderr)
        return 1
    except AaHmiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nshutting down.", file=sys.stderr)
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


_SUBCOMMANDS = ("run", "serve", "list", "forget")


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
    if command == "serve":
        return cmd_serve(args)
    if command == "list":
        return cmd_list(args)
    if command == "forget":
        return cmd_forget(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
