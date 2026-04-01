"""Unit tests for deep_research.credits module."""

from __future__ import annotations

import pytest

from deep_research.credits import CreditTracker, estimate_credits


# ── CreditTracker (in-memory SQLite) ─────────────────────────────────────


@pytest.fixture()
def tracker() -> CreditTracker:
    """Create a fresh in-memory CreditTracker for each test."""
    t = CreditTracker(db_path=":memory:")
    yield t
    t.close()


class TestCreditTrackerGetUsage:
    """CreditTracker.get_usage behaviour."""

    def test_returns_zero_for_unknown_key(self, tracker: CreditTracker) -> None:
        assert tracker.get_usage("nonexistent-key") == 0

    def test_returns_added_credits(self, tracker: CreditTracker) -> None:
        tracker.add_usage("key-a", 5)
        assert tracker.get_usage("key-a") == 5


class TestCreditTrackerAddUsage:
    """CreditTracker.add_usage behaviour."""

    def test_increments_correctly(self, tracker: CreditTracker) -> None:
        tracker.add_usage("key-a", 10)
        assert tracker.get_usage("key-a") == 10

    def test_multiple_calls_accumulate(self, tracker: CreditTracker) -> None:
        tracker.add_usage("key-a", 3)
        tracker.add_usage("key-a", 7)
        tracker.add_usage("key-a", 2)
        assert tracker.get_usage("key-a") == 12


class TestCreditTrackerGetAllUsage:
    """CreditTracker.get_all_usage behaviour."""

    def test_returns_all_keys_for_current_period(
        self, tracker: CreditTracker
    ) -> None:
        tracker.add_usage("key-a", 5)
        tracker.add_usage("key-b", 10)
        tracker.add_usage("key-c", 3)

        result = tracker.get_all_usage()

        assert result == {"key-a": 5, "key-b": 10, "key-c": 3}

    def test_different_keys_track_independently(
        self, tracker: CreditTracker
    ) -> None:
        tracker.add_usage("key-x", 100)
        tracker.add_usage("key-y", 1)

        assert tracker.get_usage("key-x") == 100
        assert tracker.get_usage("key-y") == 1
        assert tracker.get_all_usage() == {"key-x": 100, "key-y": 1}

    def test_returns_empty_dict_when_no_usage(
        self, tracker: CreditTracker
    ) -> None:
        assert tracker.get_all_usage() == {}


class TestCreditTrackerClose:
    """CreditTracker.close behaviour."""

    def test_close_does_not_raise(self) -> None:
        t = CreditTracker(db_path=":memory:")
        t.close()  # should not raise


# ── estimate_credits ─────────────────────────────────────────────────────


class TestEstimateCredits:
    """Parametrized tests for the estimate_credits function."""

    @pytest.mark.parametrize(
        ("endpoint", "params", "expected"),
        [
            # ── search ──
            pytest.param(
                "search",
                {},
                1,
                id="search-basic",
            ),
            pytest.param(
                "search",
                {"search_depth": "advanced"},
                2,
                id="search-advanced",
            ),
            # ── extract ──
            pytest.param(
                "extract",
                {"urls": ["https://example.com"]},
                1,
                id="extract-1-url-basic",
            ),
            pytest.param(
                "extract",
                {"urls": [f"https://example.com/{i}" for i in range(10)]},
                2,
                id="extract-10-urls-basic",
            ),
            pytest.param(
                "extract",
                {
                    "urls": [f"https://example.com/{i}" for i in range(10)],
                    "extract_depth": "advanced",
                },
                4,
                id="extract-10-urls-advanced",
            ),
            # ── map ──
            pytest.param(
                "map",
                {"limit": 50},
                5,
                id="map-no-instructions-limit-50",
            ),
            pytest.param(
                "map",
                {"instructions": "find pricing pages", "limit": 50},
                10,
                id="map-with-instructions-limit-50",
            ),
            # ── crawl ──
            pytest.param(
                "crawl",
                {"limit": 50},
                10,
                id="crawl-basic-limit-50",
            ),
            pytest.param(
                "crawl",
                {"limit": 50, "extract_depth": "advanced"},
                20,
                id="crawl-advanced-limit-50",
            ),
            # ── research ──
            pytest.param(
                "research",
                {"model": "pro"},
                60,
                id="research-pro",
            ),
            pytest.param(
                "research",
                {"model": "mini"},
                30,
                id="research-mini",
            ),
            pytest.param(
                "research",
                {"model": "auto"},
                45,
                id="research-auto",
            ),
            # ── unknown endpoint ──
            pytest.param(
                "totally-unknown",
                {"anything": True},
                1,
                id="unknown-endpoint",
            ),
        ],
    )
    def test_credit_estimation(
        self, endpoint: str, params: dict, expected: int
    ) -> None:
        assert estimate_credits(endpoint, params) == expected
