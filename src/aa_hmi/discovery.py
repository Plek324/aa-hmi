"""discovery.py -- classic Bluetooth device scan + pairing, via bluetoothctl.

Why bluetoothctl (subprocess) instead of PyBluez or BlueZ's D-Bus API
directly: see docs/discovery-backends.md for the full tradeoff writeup.
Short version -- bluetoothctl ships with BlueZ on any Raspberry Pi OS
install (already relied on throughout aa_pi2display's own docs), needs no
extra Python dependency, and modern versions support one-shot
non-interactive subcommands for the common path. The one place that isn't
enough is a pairing PIN/passkey confirmation prompt, which only appears on
bluetoothctl's interactive REPL -- handled by _pair_interactive() below.

This module exposes a small interface (scan / pair / is_paired / remove)
deliberately, so a future backend (e.g. a D-Bus implementation for
distros without bluetoothctl, or for headless-friendlier agent handling)
can be swapped in without touching bootstrap.py or cli.py.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from queue import Empty, Queue

from .errors import BluetoothCtlError
from .log import log

_DEVICE_LINE_RE = re.compile(r"^Device\s+([0-9A-Fa-f:]{17})\s+(.*)$")
_PAIRED_RE = re.compile(r"^\s*Paired:\s*(yes|no)\s*$", re.IGNORECASE)

# Interactive-REPL prompts we auto-confirm during pairing. Auto-confirming
# is a deliberate tradeoff, not an oversight -- these are low-security
# consumer head units, not a threat model where a rogue confirmation
# prompt is expected. See README's Security notes section. Every
# auto-confirm is logged at INFO level so it's never silent.
_AUTO_YES_PROMPTS = (
    "Confirm passkey",
    "Authorize service",
    "Accept pairing",
)


@dataclass
class BtDevice:
    mac: str
    name: str


class BluetoothCtl:
    """Thin wrapper over the bluetoothctl CLI."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def _run(self, args: list[str], timeout: float | None = None) -> str:
        try:
            proc = subprocess.run(
                ["bluetoothctl", *args],
                capture_output=True, text=True,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except FileNotFoundError as exc:
            raise BluetoothCtlError(
                "bluetoothctl not found -- is BlueZ installed? "
                "(apt install bluez on Debian/Raspberry Pi OS)"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BluetoothCtlError(f"bluetoothctl {' '.join(args)} timed out") from exc
        return proc.stdout

    def power_on(self) -> None:
        self._run(["power", "on"])

    def scan(self, duration_s: float = 10.0) -> list[BtDevice]:
        """Scan for `duration_s` seconds, then list currently-known
        devices. Listing separately (rather than parsing live scan
        output) is simpler and more robust -- bluetoothctl's scan output
        format has changed across BlueZ versions, but `devices` hasn't."""
        log(f"scanning for Bluetooth devices ({duration_s:.0f}s)...")
        self._run(["--timeout", str(int(duration_s)), "scan", "on"],
                   timeout=duration_s + self.timeout)
        out = self._run(["devices"])
        devices = []
        for line in out.splitlines():
            m = _DEVICE_LINE_RE.match(line.strip())
            if m:
                devices.append(BtDevice(mac=m.group(1), name=m.group(2).strip()))
        return devices

    def is_paired(self, mac: str) -> bool:
        out = self._run(["info", mac])
        for line in out.splitlines():
            m = _PAIRED_RE.match(line)
            if m:
                return m.group(1).lower() == "yes"
        return False

    def pair(self, mac: str, *, interactive_timeout: float = 30.0) -> bool:
        """Pair with mac. Tries a one-shot `pair` first; if that doesn't
        settle within a few seconds (most likely a passkey/authorization
        prompt only the interactive REPL can see), falls back to
        _pair_interactive()."""
        if self.is_paired(mac):
            return True
        try:
            self._run(["pair", mac], timeout=8.0)
        except BluetoothCtlError:
            pass  # fall through to interactive path below
        if self.is_paired(mac):
            self._run(["trust", mac])
            return True
        log("  one-shot pair didn't settle -- retrying interactively "
            "(auto-confirming any passkey/authorize prompt)")
        ok = self._pair_interactive(mac, timeout=interactive_timeout)
        if ok:
            self._run(["trust", mac])
        return ok

    def _pair_interactive(self, mac: str, timeout: float) -> bool:
        proc = subprocess.Popen(
            ["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        lines: Queue[str] = Queue()

        def reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        def send(cmd: str):
            assert proc.stdin is not None
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()

        ok = False
        try:
            send(f"pair {mac}")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=0.5)
                except Empty:
                    continue
                stripped = line.strip()
                if stripped:
                    log(f"  bluetoothctl: {stripped}", verbose_only=True, verbose=True)
                if any(prompt in line for prompt in _AUTO_YES_PROMPTS):
                    log(f"  auto-confirming prompt: {stripped}")
                    send("yes")
                if "Pairing successful" in line:
                    ok = True
                    break
                if "Failed to pair" in line or "org.bluez.Error" in line:
                    break
            if ok:
                send(f"trust {mac}")
                time.sleep(0.3)
        finally:
            send("quit")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return ok or self.is_paired(mac)

    def remove(self, mac: str) -> None:
        try:
            self._run(["remove", mac])
        except BluetoothCtlError:
            pass  # not paired/known -- fine
