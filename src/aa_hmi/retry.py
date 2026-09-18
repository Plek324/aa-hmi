"""retry.py -- generic retry-with-backoff, used around BT scan/pair,
per-channel RFCOMM connects, and the handshake read.

aa_pi2display's run_session.sh handles flakiness ad hoc, with bespoke
polling loops for each individual step. This is a single reusable helper
instead, parameterized so callers can give it a total time budget rather
than a fixed attempt count -- real classic-BT flakiness on this hardware
has been observed to need "patience measured in minutes," not a handful of
quick retries (see errors.HINT_FLAKY_CLASSIC_BT).
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

from .log import log

T = TypeVar("T")


class RetryExhaustedError(Exception):
    def __init__(self, attempts: int, last_exc: BaseException):
        self.attempts = attempts
        self.last_exc = last_exc
        super().__init__(f"gave up after {attempts} attempt(s); last error: {last_exc!r}")


def is_retryable_oserror(exc: BaseException) -> bool:
    from .errors import RETRYABLE_ERRNOS
    return isinstance(exc, OSError) and exc.errno in RETRYABLE_ERRNOS


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 10,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    time_budget: float | None = None,
    retryable: Callable[[BaseException], bool] | tuple[type[BaseException], ...] = (OSError,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call fn() until it succeeds, retrying on a retryable exception with
    exponential backoff (capped at max_delay), until max_attempts is
    reached OR time_budget seconds have elapsed (whichever comes first --
    time_budget=None means attempt-count-limited only).

    retryable is either an exception-type tuple (isinstance check) or a
    predicate(exc) -> bool, so callers can use errors.is_retryable_oserror
    for the errno-specific cases documented in errors.py.
    """
    start = time.monotonic()
    attempt = 0
    delay = base_delay
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, filtered below
            matches = retryable(exc) if callable(retryable) else isinstance(exc, retryable)
            elapsed = time.monotonic() - start
            out_of_attempts = attempt >= max_attempts
            out_of_time = time_budget is not None and elapsed >= time_budget
            if not matches or out_of_attempts or out_of_time:
                raise RetryExhaustedError(attempt, exc) from exc
            if on_retry:
                on_retry(attempt, exc, delay)
            else:
                log(f"  retry {attempt}/{max_attempts}: {type(exc).__name__}: {exc} "
                    f"(waiting {delay:.1f}s)", verbose_only=True, verbose=True)
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
