import pytest

from aa_hmi.retry import RetryExhaustedError, is_retryable_oserror, retry_with_backoff


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
