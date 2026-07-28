"""Tests for per-session Tavily burst protection."""

import pytest

from deep_research.burst import BurstLimitExceeded, SessionBurstLimiter


def test_rejects_calls_above_rolling_session_limit() -> None:
    now = 100.0
    limiter = SessionBurstLimiter(2, 60, clock=lambda: now)

    limiter.check("session-1")
    limiter.check("session-1")

    with pytest.raises(BurstLimitExceeded, match="2 calls per 60s"):
        limiter.check("session-1")


def test_sessions_have_independent_budgets() -> None:
    limiter = SessionBurstLimiter(1, 60, clock=lambda: 100.0)

    limiter.check("session-1")
    limiter.check("session-2")


def test_budget_recovers_after_window() -> None:
    now = 100.0
    limiter = SessionBurstLimiter(1, 60, clock=lambda: now)
    limiter.check("session-1")

    now = 160.0
    limiter.check("session-1")


def test_missing_session_or_zero_limit_is_not_globally_throttled() -> None:
    limiter = SessionBurstLimiter(1, 60, clock=lambda: 100.0)
    limiter.check(None)
    limiter.check(None)

    disabled = SessionBurstLimiter(0, 60, clock=lambda: 100.0)
    disabled.check("session-1")
    disabled.check("session-1")


def test_session_tracking_is_memory_bounded() -> None:
    limiter = SessionBurstLimiter(1, 60, clock=lambda: 100.0, max_sessions=3)

    for index in range(10):
        limiter.check(f"session-{index}")

    assert list(limiter._requests) == ["session-7", "session-8", "session-9"]
