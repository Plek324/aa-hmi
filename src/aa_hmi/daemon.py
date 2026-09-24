"""daemon.py -- `aa-hmi serve`: the full display-server daemon.

Orchestrates, in order: device resolution + pairing (orchestrate.py) ->
WiFi-credential bootstrap + connect (orchestrate.py, wifi.py, inside
coexistence.py's WiFi-disconnect-for-BT workaround) -> cert.ensure_cert
-> a VideoSession (video_session.py) -> an IpcServer (ipc/server.py)
bridging FRAME-in/TOUCH-out between that session and any connected client
program.

Reconnect policy: on ANY drop of the video session (TLS error, TCP EOF,
reader thread died), tear it down cleanly and re-run the ENTIRE bootstrap
sequence from device resolution -- full Bluetooth re-trigger, full WiFi
re-bootstrap. This is the safe default given docs/video-protocol-notes.md's
still-open question of whether one Bluetooth trigger can hold a TCP
session open indefinitely or whether periodic re-arming turns out to be
needed: always re-arming is correct either way, just potentially wasteful
if a lighter-weight reconnect would have worked. The IPC server itself is
NOT torn down on a video-session drop -- a connected client keeps its
socket; frames sent during the reconnect window are just dropped (with a
rate-limited log) rather than erroring the client's connection.

Final shutdown (Ctrl+C / SIGTERM, not an automatic reconnect cycle) also
disconnects and forgets the display's WiFi network -- see
_disconnect_wifi_on_shutdown's docstring for why this matters (found
live, 2026-09-22: skipping it broke the next `aa-hmi serve` start).
"""
from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path

from . import cache, cert, coexistence, encoder, orchestrate, wifi
from .discovery import BluetoothCtl
from .errors import HINT_VIDEO_SESSION_FLAKY
from .ipc import protocol as ipc_wire
from .ipc.server import IpcServer
from .log import log
from .retry import RetryCancelledError
from .touch_channel import parse_touch_event
from .video_session import VideoSession

DEFAULT_ASSUME_PERSISTENT_SESSION = False
_FRAME_DROP_LOG_INTERVAL = 5.0  # seconds between "dropping frame, no session" log lines


class _SessionHolder:
    """Mutable box so the IPC server's on_frame callback (registered once,
    before the reconnect loop starts) can always reach whichever
    VideoSession is current, across reconnects, without re-registering a
    new callback each time."""

    def __init__(self, timestamp_mode: str = "elapsed", idr_alternation: bool = False,
                 single_slice: bool = True):
        self.session: VideoSession | None = None
        self.rfcomm_sock = None  # held open alongside `session` -- see _bootstrap_and_open_session
        self.frame_counter = 0   # access units (slices) sent this session
        self.image_counter = 0   # client images sent this session
        self.session_start = 0.0  # time.monotonic() when the current session opened
        # "elapsed": every slice of one image gets the same timestamp =
        # real microseconds since the session opened. "per-slice": the
        # original behavior, +33333us per slice regardless of real time --
        # kept for A/B testing a freeze (see docs/video-protocol-notes.md).
        self.timestamp_mode = timestamp_mode
        # Alternate idr_pic_id 0/1 between consecutive images, as the H.264
        # spec requires (see encoder.encode_frame_to_access_units).
        self.idr_alternation = idr_alternation
        # One slice (= one media message) per image, like a real phone.
        self.single_slice = single_slice
        self.reconnect_count = 0
        self.last_ssid: str | None = None  # for run_daemon's final-shutdown WiFi cleanup
        self._last_drop_log = 0.0

    def close_session(self) -> None:
        """Closes both the video session and the RFCOMM socket held open
        alongside it (see _bootstrap_and_open_session for why the latter
        must stay open through video-session setup) -- always call this
        instead of closing session/rfcomm_sock separately, so the two
        never get out of sync."""
        if self.session is not None:
            self.session.close()
            self.session = None
        if self.rfcomm_sock is not None:
            try:
                self.rfcomm_sock.close()
            except OSError:
                pass
            self.rfcomm_sock = None

    def image_timestamp(self) -> int:
        """Microseconds since the current session opened -- still
        stream-relative (the display has no RTC; epoch time is what broke
        things originally), but tracking real elapsed time instead of a
        fixed +33333us per slice, which drifted ~0.87s behind real time
        per second at 1 image/s with 4 slices per image."""
        return int((time.monotonic() - self.session_start) * 1_000_000)

    def on_frame(self, frame_msg: ipc_wire.FrameMsg) -> None:
        session = self.session
        if session is None or not session.is_alive():
            now = time.monotonic()
            if now - self._last_drop_log > _FRAME_DROP_LOG_INTERVAL:
                log("dropping a client frame -- no active video session right now (reconnecting?)")
                self._last_drop_log = now
            return
        try:
            parity = self.image_counter % 2 if self.idr_alternation else 0
            access_units = encoder.encode_frame_to_access_units(frame_msg.pixel_data, frame_msg.width, frame_msg.height,
                                                                idr_pic_id_parity=parity,
                                                                single_slice=self.single_slice)
            self.image_counter += 1
            image_ts = self.image_timestamp()
            sizes = ", ".join(str(sum(len(n) + 4 for n in au)) for au in access_units)
            ts = image_ts
            for au in access_units:
                nal_bytes = encoder.assemble_nal_bytes(au)
                self.frame_counter += 1
                ts = image_ts if self.timestamp_mode == "elapsed" else self.frame_counter * 33333
                session.send_frame(nal_bytes, ts)
            log(f"  sent image #{self.image_counter} as {len(access_units)} message(s) "
                f"({sizes} bytes), idr_pic_id={parity}, ts={ts}us "
                f"(messages sent this session: {self.frame_counter})",
                verbose_only=True, verbose=True)
        except Exception as e:  # noqa: BLE001 -- one bad frame must never kill the daemon
            log(f"failed to encode/send a client frame: {type(e).__name__}: {e}")


def _cache_path(args) -> Path:
    return Path(args.config_dir) if args.config_dir else cache.default_cache_path()


def _cert_paths(args, cache_path: Path) -> tuple[Path, Path]:
    default_dir = cache_path.parent / "certs"
    cert_file = Path(args.cert) if args.cert else default_dir / "fake_phone_cert.pem"
    key_file = Path(args.key) if args.key else default_dir / "fake_phone_key.pem"
    return cert_file, key_file


def _bootstrap_and_open_session(args, bt: BluetoothCtl, cache_path: Path,
                                 cert_file: Path, key_file: Path,
                                 ipc_server: IpcServer, holder: _SessionHolder,
                                 stop_event: threading.Event) -> VideoSession:
    selection = orchestrate.DeviceSelectionArgs(
        device=args.device, rescan=args.rescan,
        non_interactive=args.non_interactive, scan_duration=args.scan_duration,
    )
    mac, cached = orchestrate.resolve_device(selection, bt, cache_path)

    bt.power_on()
    if not bt.pair(mac):
        raise SystemExit(f"error: failed to pair with {mac}")

    rfcomm_sock = None
    with coexistence.wifi_disconnected_for_bluetooth(
        args.wifi_iface, enabled=args.radio_coexistence_workaround
    ):
        # confirm_connected=True: keeps the RFCOMM/Bluetooth connection
        # OPEN (returned as rfcomm_sock) rather than closing it right
        # away. Confirmed live (2026-09-18): the display's TCP video port
        # only accepts a connection while this link is still open --
        # closing it immediately after the WiFi handshake (as `aa-hmi
        # run` correctly does for its own narrower scope) left the video
        # port refusing every connection attempt. See
        # orchestrate.bootstrap_wifi_info's docstring and
        # docs/video-protocol-notes.md. Held open for this whole session's
        # lifetime out of caution (only confirmed it must be open AT
        # TCP-connect time, not how much longer beyond that is actually
        # required) -- closed in run_daemon's cleanup alongside the video
        # session itself.
        info, channel_used, method, rfcomm_sock = orchestrate.bootstrap_wifi_info(
            mac, args.channel, cached, args.bt_timeout, confirm_connected=True, cancel_event=stop_event,
        )
        cache.upsert_device(
            cache_path, mac,
            rfcomm_channel=channel_used, discovery_method=method,
            last_ssid=info.ssid, last_key=info.key, last_bssid=info.bssid,
            last_security_mode_raw=info.security_mode_raw,
            last_seen=cache.now_iso(), last_success=cache.now_iso(),
        )
        wifi.connect(info.ssid, info.key, iface=args.wifi_iface)
        holder.last_ssid = info.ssid  # so run_daemon's final shutdown can disconnect/forget it

    cert.ensure_cert(cert_file, key_file)

    try:
        session = VideoSession(args.display_ip, cert_file, key_file, verbose=args.verbose)
        session.connect_and_handshake(timeout=10.0)
        session.open_video_channel()
        session.open_touch_channel(lambda raw: ipc_server.broadcast_touch(parse_touch_event(raw)))
    except Exception:
        if rfcomm_sock is not None:
            rfcomm_sock.close()
        raise

    holder.session = session
    holder.rfcomm_sock = rfcomm_sock
    holder.frame_counter = 0
    holder.image_counter = 0
    holder.session_start = time.monotonic()
    return session


def _disconnect_wifi_on_shutdown(args, holder: _SessionHolder) -> None:
    """Only called once, on run_daemon's actual final exit (not between
    automatic reconnect cycles, which should stay connected) -- mirrors
    aa_pi2display's run_session.sh, which always disconnected AND forgot
    the display's WiFi network on its way out. daemon.py had been
    skipping this entirely (an oversight, not a deliberate choice):
    reported live (2026-09-22) that Ctrl+C left the Pi connected to the
    display's AP, and restarting `aa-hmi serve` then failed to
    reconnect -- almost certainly the documented WiFi/Bluetooth
    radio-coexistence issue on the Pi's onboard combo chip (active WiFi
    starves outbound Bluetooth), since the next run's Bluetooth bootstrap
    has no way to know it should disconnect WiFi first unless
    --radio-coexistence-workaround happens to be set. Disconnecting (and
    forgetting the stale connection profile, same rationale as
    wifi.delete_stale_profile) here means every `aa-hmi serve` start
    behaves the same regardless of how the previous run ended.
    Best-effort: logged, never allowed to raise past this point -- a
    failure to clean up WiFi must never mask a real shutdown."""
    if holder.last_ssid is None:
        return
    try:
        wifi.disconnect(args.wifi_iface)
        wifi.forget(holder.last_ssid)
        log(f"disconnected and forgot WiFi network {holder.last_ssid!r} on the way out")
    except Exception as e:  # noqa: BLE001 -- best-effort cleanup, must never mask a real shutdown
        log(f"WiFi cleanup on shutdown failed (continuing anyway): {type(e).__name__}: {e}")


def _format_ack_stats(stats: dict) -> str:
    def secs(v):
        return "never" if v is None else f"{v:.1f}s ago"
    return (f"media flow: sent={stats['frames_sent']} acked={stats['acks_received']} "
            f"outstanding={stats['outstanding']} max_unacked={stats['max_unacked']} "
            f"last send {secs(stats['seconds_since_send'])}, last ack {secs(stats['seconds_since_ack'])}")


def _wait_until_dropped_or_stopped(session: VideoSession, stop_event: threading.Event,
                                    liveness_timeout: float | None, poll_interval: float = 1.0,
                                    stats_interval: float | None = None) -> None:
    """stats_interval (set from -v) prints a media-flow summary that often
    -- added to catch the moment the display stops acking, since a freeze
    otherwise leaves nothing in the log."""
    next_stats = time.monotonic() + stats_interval if stats_interval else None
    while not stop_event.is_set():
        if next_stats is not None and time.monotonic() >= next_stats:
            log(_format_ack_stats(session.ack_stats()))
            next_stats = time.monotonic() + stats_interval
        if not session.is_alive(liveness_timeout=liveness_timeout):
            if session.is_alive():  # transport-level fine, so it must be the liveness check that tripped
                log(f"video session transport looks fine but the display hasn't sent anything in "
                    f"{session.seconds_since_last_activity():.0f}s (>{liveness_timeout:.0f}s liveness timeout) -- "
                    f"treating as wedged, will reconnect")
            else:
                log("video session no longer alive, will reconnect")
            return
        stop_event.wait(poll_interval)


def run_daemon(args) -> int:
    cache_path = _cache_path(args)
    cert_file, key_file = _cert_paths(args, cache_path)
    bt = BluetoothCtl(timeout=max(10.0, args.bt_timeout / 6))

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        log(f"received signal {signum}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    holder = _SessionHolder(timestamp_mode=getattr(args, "timestamp_mode", "elapsed"),
                            idr_alternation=getattr(args, "idr_alternation", False),
                            single_slice=getattr(args, "single_slice", True))
    ipc_server = IpcServer(Path(args.socket_path) if args.socket_path else None)
    ipc_server.start(on_frame=holder.on_frame)

    consecutive_failures = 0
    try:
        while not stop_event.is_set():
            try:
                session = _bootstrap_and_open_session(args, bt, cache_path, cert_file, key_file, ipc_server,
                                                       holder, stop_event)
                if consecutive_failures > 0 and args.persistent_session:
                    log(f"WARNING: reconnected after {consecutive_failures} failure(s) despite "
                        f"--persistent-session -- this is a real data point for the soak-test "
                        f"question in docs/video-protocol-notes.md, please report it")
                consecutive_failures = 0
                holder.reconnect_count += 1
                log(f"=== display session established (connection #{holder.reconnect_count}), serving IPC ===")
                liveness_timeout = args.liveness_timeout if args.liveness_timeout and args.liveness_timeout > 0 else None
                _wait_until_dropped_or_stopped(session, stop_event, liveness_timeout,
                                               stats_interval=10.0 if args.verbose else None)
            except RetryCancelledError:
                # stop_event fired while a bootstrap attempt was retrying/backing
                # off -- see retry.retry_with_backoff's cancel_event docstring.
                # Not a real failure, just the shutdown path; log plainly and
                # let the outer `while not stop_event.is_set()` exit cleanly.
                log("bootstrap cancelled (shutting down)")
            except Exception as e:  # noqa: BLE001 -- a failed attempt must go through the retry/backoff path, not crash the daemon
                consecutive_failures += 1
                log(f"session attempt failed ({consecutive_failures} in a row): {type(e).__name__}: {e}")
                if args.reconnect_max_attempts and consecutive_failures >= args.reconnect_max_attempts:
                    print(f"\n{HINT_VIDEO_SESSION_FLAKY}", file=sys.stderr)
                    return 1
            finally:
                holder.close_session()
            if not stop_event.is_set():
                delay = min(5.0 * max(consecutive_failures, 1), 30.0) if consecutive_failures else 2.0
                log(f"reconnecting in {delay:.0f}s...")
                stop_event.wait(delay)
    finally:
        ipc_server.stop()
        holder.close_session()
        _disconnect_wifi_on_shutdown(args, holder)
    return 0
