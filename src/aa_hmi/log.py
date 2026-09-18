"""log.py -- shared elapsed-time console logger.

Mirrors the log() helper in aa_pi2display's aa_session.py: every line is
prefixed with seconds-since-start, which makes it far easier to correlate
console output with a tcpdump/bluetoothctl capture running alongside it.
"""
import sys
import time

_start = time.time()


def log(msg: str, *, verbose_only: bool = False, verbose: bool = True) -> None:
    """Print msg prefixed with elapsed time.

    verbose_only=True lines are skipped unless verbose is True -- callers
    typically pass the CLI's --verbose flag through as `verbose`.
    """
    if verbose_only and not verbose:
        return
    print(f"[{time.time() - _start:8.3f}s] {msg}", file=sys.stderr, flush=True)


def reset() -> None:
    """Reset the elapsed-time origin -- mainly useful in tests."""
    global _start
    _start = time.time()
