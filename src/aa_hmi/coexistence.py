"""coexistence.py -- optional workaround for combo WiFi+Bluetooth chips
(e.g. the Raspberry Pi's onboard Broadcom BCM4345C0) that can't use both
radios well at once: active WiFi has been observed to starve outbound
Bluetooth connectivity entirely (l2ping reliably failing with "Host is
down" while WiFi-connected, working immediately on disconnect -- see
aa_pi2display's README "Known quirks").

This is opt-in (--radio-coexistence-workaround), off by default, since
it's specific to certain client hardware, not universal, and
unconditionally disconnecting the caller's WiFi would be a surprising
thing for a general-purpose tool to do without being asked.
"""
from __future__ import annotations

from contextlib import contextmanager

from . import wifi
from .log import log


@contextmanager
def wifi_disconnected_for_bluetooth(iface: str | None = None, *, enabled: bool = True):
    """If enabled, disconnect WiFi on entry and leave it disconnected on
    exit -- reconnecting is the caller's job (normally via wifi.connect()
    with the freshly-bootstrapped credentials right after this context
    exits), mirroring run_session.sh's own sequencing."""
    if not enabled:
        yield
        return
    target = iface or wifi.find_wifi_iface()
    log(f"  radio-coexistence workaround: disconnecting WiFi ({target}) "
        f"to free the Bluetooth radio")
    wifi.disconnect(target)
    try:
        yield
    finally:
        pass  # reconnect is the caller's responsibility, not ours
