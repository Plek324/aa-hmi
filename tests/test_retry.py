import threading

import pytest

from aa_hmi.retry import RetryCancelledError, RetryExhaustedError, is_retryable_oserror, retry_with_backoff


def test_succeeds_first_try():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert retry_with_backoff(fn, base_delay=0.0) == "ok"
    assert len(calls) == 1


def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("flaky")
        return "ok"

    result = retry_with_backoff(fn, max_attempts=5, base_delay=0.0, retryable=(OSError,))
    assert result == "ok"
    assert calls["n"] == 3


def test_raises_retry_exhausted_after_max_attempts(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def fn():
        raise OSError("always fails")

    with pytest.raises(RetryExhaustedError) as excinfo:
        retry_with_backoff(fn, max_attempts=3, base_delay=0.0, retryable=(OSError,))
    assert excinfo.value.attempts == 3


def test_non_retryable_exception_raises_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(fn, max_attempts=5, base_delay=0.0, retryable=(OSError,))
    assert calls["n"] == 1  # gave up immediately, didn't retry a non-matching exception


def test_predicate_retryable_used_to_filter_by_errno(monkeypatch):
    """A predicate (not just an exception-type tuple) can be passed as
    `retryable` -- here, is_retryable_oserror, which only matches specific
    errno values (see errors.RETRYABLE_ERRNOS)."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    def fn():
        exc = OSError("not one of the known-flaky errnos")
        exc.errno = 2  # ENOENT -- not in RETRYABLE_ERRNOS
        raise exc

    with pytest.raises(RetryExhaustedError) as excinfo:
        retry_with_backoff(fn, max_attempts=5, base_delay=0.0, retryable=is_retryable_oserror)
    assert excinfo.value.attempts == 1  # not retried at all -- errno didn't match


def test_is_retryable_oserror_filters_by_errno():
    matching = OSError()
    matching.errno = 112  # EHOSTDOWN
    assert is_retryable_oserror(matching) is True

    other = OSError()
    other.errno = 2  # ENOENT
    assert is_retryable_oserror(other) is False


def test_time_budget_stops_retrying(monkeypatch):
    fake_time = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: fake_time["t"])

    def fake_sleep(s):
        fake_time["t"] += s

    monkeypatch.setattr("time.sleep", fake_sleep)

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise OSError("always fails")

    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(fn, max_attempts=1000, base_delay=1.0, max_delay=1.0,
                            time_budget=5.0, retryable=(OSError,))
    assert calls["n"] < 1000  # bailed out on time budget, not attempt count


def test_cancel_event_already_set_bails_before_first_attempt():
    """Regression test for a real daemon shutdown-latency bug found live
    (2026-09-18): aa-hmi serve's SIGTERM handler could set stop_event and
    the daemon would still grind through an entire in-progress retry
    budget (including starting brand new attempts) before ever noticing.
    If cancel_event is already set, fn() must never even be called once."""
    cancel_event = threading.Event()
    cancel_event.set()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "should never happen"

    with pytest.raises(RetryCancelledError):
        retry_with_backoff(fn, cancel_event=cancel_event)
    assert calls["n"] == 0


def test_cancel_event_set_during_backoff_wait_cancels_immediately(monkeypatch):
    """The other half of the fix: a cancel_event that fires WHILE waiting
    out a backoff delay must interrupt that wait immediately, not sleep
    out the full delay first."""
    cancel_event = threading.Event()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            cancel_event.set()  # simulates a signal handler firing mid-retry
            raise OSError("transient, but we're about to be cancelled")
        raise AssertionError("must not retry a second time once cancelled")

    with pytest.raises(RetryCancelledError) as excinfo:
        retry_with_backoff(fn, max_attempts=5, base_delay=100.0,  # huge delay -- must NOT actually wait it out
                            retryable=(OSError,), cancel_event=cancel_event)
    assert calls["n"] == 1
    assert excinfo.value.attempts == 1


def test_no_cancel_event_behaves_exactly_as_before(monkeypatch):
    """cancel_event=None (the default) must not change existing behavior
    at all -- every other test in this file already covers that
    implicitly by never passing it, this just makes the guarantee
    explicit."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("flaky")
        return "ok"

    assert retry_with_backoff(fn, max_attempts=5, base_delay=0.0, retryable=(OSError,)) == "ok"
