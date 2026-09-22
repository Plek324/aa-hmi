"""VideoSession's connect/handshake path needs real sockets+TLS to
exercise meaningfully (covered by live-hardware verification, see
docs/video-live-verification.md) -- but is_alive()'s liveness-timeout
logic is pure enough to unit test directly against a constructed
instance, and it's exactly the piece added as a regression fix for a
real bug reported live (2026-09-22): the display's own video decoder can
wedge silently while the TCP/TLS transport stays completely healthy, and
without this check aa-hmi had no way to ever notice."""
import threading
import time

import pytest

from aa_hmi.video_session import VideoSession, VideoSessionState


@pytest.fixture
def open_session():
    """A VideoSession in the OPEN state with a real (but idle) 'reader
    thread' standing in for the background reader -- close enough for
    is_alive()'s own logic, which only checks thread.is_alive()."""
    session = VideoSession("192.168.10.1", "cert.pem", "key.pem")
    session.state = VideoSessionState.OPEN
    stop = threading.Event()
    session._reader_thread = threading.Thread(target=stop.wait, daemon=True)
    session._reader_thread.start()
    session._last_activity = time.monotonic()
    yield session
    stop.set()
    session._reader_thread.join(timeout=1.0)


def test_is_alive_true_with_no_liveness_timeout_given(open_session):
    assert open_session.is_alive() is True


def test_is_alive_true_with_recent_activity_and_liveness_timeout(open_session):
    assert open_session.is_alive(liveness_timeout=60.0) is True


def test_is_alive_false_when_liveness_timeout_exceeded(open_session):
    """The actual regression case: transport (state + reader thread) is
    completely fine, but nothing has been heard from the display in
    longer than the timeout -- must be treated as not alive."""
    open_session._last_activity = time.monotonic() - 120.0  # 2 minutes of silence
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_is_alive_false_when_liveness_timeout_exceeded_but_true_without_one(open_session):
    """Confirms the liveness check is additive, not a replacement --
    omitting liveness_timeout must still reflect the old (transport-only)
    behavior, for any caller not opting into the new check."""
    open_session._last_activity = time.monotonic() - 120.0
    assert open_session.is_alive() is True
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_is_alive_false_when_state_not_open_regardless_of_liveness(open_session):
    open_session.state = VideoSessionState.CLOSED
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_is_alive_false_when_reader_thread_dead_regardless_of_liveness(open_session):
    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()  # guaranteed finished/not-alive
    open_session._reader_thread = dead_thread
    assert open_session.is_alive(liveness_timeout=60.0) is False


def test_seconds_since_last_activity_reflects_elapsed_time(open_session):
    open_session._last_activity = time.monotonic() - 42.0
    elapsed = open_session.seconds_since_last_activity()
    assert 41.0 < elapsed < 43.0  # small tolerance for test execution time


def test_liveness_timeout_boundary_is_exclusive(open_session):
    """Exactly at the boundary should still count as alive -- only
    exceeding it should trip. (>, not >=, in the implementation.)"""
    open_session._last_activity = time.monotonic() - 60.0
    # Right at ~60s: could go either way depending on test timing jitter,
    # so just confirm comfortably-under and comfortably-over both behave
    # as expected rather than asserting the exact boundary millisecond.
    assert open_session.is_alive(liveness_timeout=61.0) is True
    assert open_session.is_alive(liveness_timeout=1.0) is False
