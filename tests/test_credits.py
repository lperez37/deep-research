"""Unit tests for deep_research.credits module."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

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

    def test_returns_all_keys_for_current_period(self, tracker: CreditTracker) -> None:
        tracker.add_usage("key-a", 5)
        tracker.add_usage("key-b", 10)
        tracker.add_usage("key-c", 3)

        result = tracker.get_all_usage()

        assert result == {"key-a": 5, "key-b": 10, "key-c": 3}

    def test_different_keys_track_independently(self, tracker: CreditTracker) -> None:
        tracker.add_usage("key-x", 100)
        tracker.add_usage("key-y", 1)

        assert tracker.get_usage("key-x") == 100
        assert tracker.get_usage("key-y") == 1
        assert tracker.get_all_usage() == {"key-x": 100, "key-y": 1}

    def test_returns_empty_dict_when_no_usage(self, tracker: CreditTracker) -> None:
        assert tracker.get_all_usage() == {}


class TestCreditTrackerCooldown:
    """CreditTracker.get_cooldown / set_cooldown behaviour."""

    def test_returns_zero_for_unknown_key(self, tracker: CreditTracker) -> None:
        assert tracker.get_cooldown("never-cooled-key") == 0

    def test_set_then_get_roundtrip(self, tracker: CreditTracker) -> None:
        tracker.set_cooldown("key-a", 1_700_000_000)
        assert tracker.get_cooldown("key-a") == 1_700_000_000

    def test_set_overwrites_previous_value(self, tracker: CreditTracker) -> None:
        tracker.set_cooldown("key-a", 1_700_000_000)
        tracker.set_cooldown("key-a", 1_800_000_000)
        assert tracker.get_cooldown("key-a") == 1_800_000_000

    def test_cooldown_is_per_key(self, tracker: CreditTracker) -> None:
        tracker.set_cooldown("key-a", 1_700_000_000)
        tracker.set_cooldown("key-b", 1_800_000_000)
        assert tracker.get_cooldown("key-a") == 1_700_000_000
        assert tracker.get_cooldown("key-b") == 1_800_000_000

    def test_cooldown_independent_of_usage(self, tracker: CreditTracker) -> None:
        """Setting cooldown must not affect the usage counter, and vice versa."""
        tracker.add_usage("key-a", 50)
        tracker.set_cooldown("key-a", 1_700_000_000)
        assert tracker.get_usage("key-a") == 50
        assert tracker.get_cooldown("key-a") == 1_700_000_000


class TestCreditTrackerClose:
    """CreditTracker.close behaviour."""

    def test_close_does_not_raise(self) -> None:
        t = CreditTracker(db_path=":memory:")
        t.close()  # should not raise


class TestRequestAuditLog:
    """Persistent requester attribution and request outcome behaviour."""

    def test_start_and_finish_request(self, tracker: CreditTracker) -> None:
        audit_id = tracker.start_request(
            endpoint="search",
            query="vertical market software",
            target=None,
            requester={
                "request_id": "req-1",
                "session_id": "session-1",
                "requester_id": "luis",
                "hostname": "workstation",
                "application": "Hermes",
                "application_version": "1.2.3",
                "source_ip": "192.0.2.10",
                "user_agent": "test-client/1.0",
                "transport": "streamable-http",
            },
        )
        tracker.finish_request(
            audit_id,
            status="succeeded",
            credits=2,
            attempts=1,
            duration_ms=42,
        )

        row = tracker.get_recent_requests()[0]
        assert row["query"] == "vertical market software"
        assert row["hostname"] == "workstation"
        assert row["application"] == "Hermes"
        assert row["application_version"] == "1.2.3"
        assert row["requester_id"] == "luis"
        assert row["status"] == "succeeded"
        assert row["credits"] == 2
        assert row["attempts"] == 1
        assert row["duration_ms"] == 42
        assert row["error_code"] is None
        assert row["completed_at"] is not None

    def test_failed_request_keeps_query_and_error_code(
        self, tracker: CreditTracker
    ) -> None:
        audit_id = tracker.start_request(
            endpoint="search",
            query="sensitive exact query",
            target=None,
            requester={},
        )
        tracker.finish_request(
            audit_id,
            status="failed",
            attempts=3,
            duration_ms=100,
            error_code="tavily_http_429",
        )

        row = tracker.get_recent_requests()[0]
        assert row["query"] == "sensitive exact query"
        assert row["status"] == "failed"
        assert row["attempts"] == 3
        assert row["error_code"] == "tavily_http_429"

    def test_invalid_finish_status_is_rejected(self, tracker: CreditTracker) -> None:
        audit_id = tracker.start_request(
            endpoint="search", query="q", target=None, requester={}
        )
        with pytest.raises(ValueError, match="status"):
            tracker.finish_request(
                audit_id,
                status="unknown",
                attempts=0,
                duration_ms=0,
            )

    def test_existing_database_is_upgraded_without_losing_usage(self, tmp_path) -> None:
        db_path = tmp_path / "credits.db"
        first = CreditTracker(str(db_path))
        first.add_usage("key-a", 7)
        first.close()

        reopened = CreditTracker(str(db_path))
        try:
            assert reopened.get_usage("key-a") == 7
            assert reopened.get_recent_requests() == []
        finally:
            reopened.close()

    def test_pre_feature_schema_is_migrated_without_losing_usage(
        self, tmp_path
    ) -> None:
        db_path = tmp_path / "legacy.db"
        connection = sqlite3.connect(db_path)
        connection.execute(
            "CREATE TABLE usage (key_id TEXT NOT NULL, period TEXT NOT NULL, "
            "used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (key_id, period))"
        )
        connection.execute(
            "CREATE TABLE cooldown (key_id TEXT PRIMARY KEY, "
            "cooldown_until INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO usage (key_id, period, used) VALUES (?, ?, ?)",
            ("legacy-key", datetime.now(UTC).strftime("%Y-%m"), 11),
        )
        connection.commit()
        connection.close()

        tracker = CreditTracker(str(db_path))
        try:
            assert tracker.get_usage("legacy-key") == 11
            request_id = tracker.start_request(
                endpoint="search",
                query="migration check",
                target=None,
                requester={},
            )
            assert request_id == 1
        finally:
            tracker.close()

    def test_database_files_are_owner_only(self, tmp_path) -> None:
        db_path = tmp_path / "credits.db"
        tracker = CreditTracker(str(db_path))
        try:
            tracker.add_usage("key-a", 1)
            paths = [db_path, tmp_path / "credits.db-wal", tmp_path / "credits.db-shm"]
            assert all((path.stat().st_mode & 0o777) == 0o600 for path in paths)
        finally:
            tracker.close()

    def test_shared_connection_serialises_concurrent_writers(self) -> None:
        tracker = CreditTracker(":memory:")

        def write_request(index: int) -> int:
            request_id = tracker.start_request(
                endpoint="search",
                query=str(index),
                target=None,
                requester={},
            )
            tracker.finish_request(
                request_id,
                status="succeeded",
                attempts=1,
                duration_ms=1,
            )
            return request_id

        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                request_ids = list(executor.map(write_request, range(100)))
            assert len(set(request_ids)) == 100
            assert len(tracker.get_recent_requests(limit=100)) == 100
        finally:
            tracker.close()

    def test_stale_started_request_is_reconciled_on_restart(self, tmp_path) -> None:
        db_path = tmp_path / "credits.db"
        tracker = CreditTracker(str(db_path), request_log_retention_days=0)
        request_id = tracker.start_request(
            endpoint="search",
            query="interrupted",
            target=None,
            requester={},
        )
        tracker.close()

        connection = sqlite3.connect(db_path)
        connection.execute(
            "UPDATE request_log SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", request_id),
        )
        connection.commit()
        connection.close()

        reopened = CreditTracker(str(db_path), request_log_retention_days=0)
        try:
            row = reopened.get_recent_requests()[0]
            assert row["status"] == "abandoned"
            assert row["completed_at"] is not None
            assert row["error_code"] == "process_restart"
        finally:
            reopened.close()

    def test_completed_request_is_pruned_on_startup(self, tmp_path) -> None:
        db_path = tmp_path / "credits.db"
        tracker = CreditTracker(str(db_path), request_log_retention_days=0)
        request_id = tracker.start_request(
            endpoint="search",
            query="expired",
            target=None,
            requester={},
        )
        tracker.finish_request(
            request_id,
            status="succeeded",
            attempts=1,
            duration_ms=1,
        )
        tracker._conn.execute(
            "UPDATE request_log SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", request_id),
        )
        tracker._conn.commit()
        tracker.close()

        reopened = CreditTracker(str(db_path), request_log_retention_days=90)
        try:
            assert reopened.get_recent_requests() == []
        finally:
            reopened.close()

    def test_retention_runs_periodically_without_restart(
        self, tracker: CreditTracker
    ) -> None:
        expired_id = tracker.start_request(
            endpoint="search",
            query="expired",
            target=None,
            requester={},
        )
        tracker.finish_request(
            expired_id,
            status="succeeded",
            attempts=1,
            duration_ms=1,
        )
        tracker._conn.execute(
            "UPDATE request_log SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", expired_id),
        )
        tracker._conn.commit()
        tracker._request_log_retention_days = 90
        tracker._next_request_log_maintenance = 0

        current_id = tracker.start_request(
            endpoint="search",
            query="current",
            target=None,
            requester={},
        )

        rows = tracker.get_recent_requests()
        assert [row["id"] for row in rows] == [current_id]
class TestFallbackSpend:
    def test_reserve_settle_and_limit(self) -> None:
        tracker = CreditTracker(":memory:")
        day = tracker.reserve_fallback_cost(0.01, 0.02)
        assert day is not None
        assert tracker.get_fallback_spend() == 0.01
        tracker.settle_fallback_cost(day, 0.01, 0.0042)
        assert tracker.get_fallback_spend() == 0.0042
        assert tracker.reserve_fallback_cost(0.01, 0.01) is None


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
