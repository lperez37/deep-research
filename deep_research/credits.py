"""SQLite-backed credit tracker with automatic monthly reset and request audit log."""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

_REQUEST_LOG_MAINTENANCE_INTERVAL_SECONDS = 24 * 60 * 60


def _current_period() -> str:
    """Return current billing period as 'YYYY-MM'."""
    return datetime.now(UTC).strftime("%Y-%m")


def _utc_now() -> str:
    """Return an unambiguous UTC timestamp for SQLite text storage."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _locked(method):
    """Serialise access to the shared SQLite connection."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _current_day() -> str:
    """Return the current UTC day as ``YYYY-MM-DD``."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


class CreditTracker:
    """Tracks credit usage, cooldowns and requester audit events in SQLite.

    Three tables:
      - ``usage`` records monthly credit consumption per key.
      - ``cooldown`` records per-key 429 cooldown expiry timestamps.
      - ``request_log`` records attributed Tavily request lifecycle events.

    Schema creation is additive and idempotent so existing ``credits.db`` files
    are upgraded automatically. A re-entrant in-process lock serialises access to
    the shared connection and mutations are committed atomically. SQLite WAL
    mode provides coordination with other processes.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        busy_timeout_seconds: float = 30.0,
        request_log_retention_days: int = 90,
    ) -> None:
        if busy_timeout_seconds < 0:
            raise ValueError("busy_timeout_seconds cannot be negative")
        if request_log_retention_days < 0:
            raise ValueError("request_log_retention_days cannot be negative")
        self._lock = threading.RLock()
        path = Path(db_path)
        if db_path != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(
                    path, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o600
                )
            except FileExistsError:
                path.chmod(0o600)
            else:
                os.close(descriptor)
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            timeout=busy_timeout_seconds,
        )
        busy_timeout_ms = round(busy_timeout_seconds * 1000)
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._enable_wal()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage (
                key_id  TEXT    NOT NULL,
                period  TEXT    NOT NULL,
                used    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (key_id, period)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldown (
                key_id          TEXT    PRIMARY KEY,
                cooldown_until  INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_log (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at           TEXT    NOT NULL,
                completed_at         TEXT,
                endpoint             TEXT    NOT NULL,
                query                TEXT,
                target               TEXT,
                request_id           TEXT,
                session_id           TEXT,
                requester_id         TEXT,
                hostname             TEXT,
                application          TEXT,
                application_version  TEXT,
                source_ip            TEXT,
                user_agent           TEXT,
                transport            TEXT,
                status               TEXT    NOT NULL DEFAULT 'started',
                credits              INTEGER,
                attempts             INTEGER NOT NULL DEFAULT 0,
                duration_ms          INTEGER,
                error_code           TEXT,
                fallback_used        INTEGER NOT NULL DEFAULT 0,
                fallback_provider    TEXT,
                fallback_cost_microusd INTEGER,
                fallback_items_returned INTEGER,
                fallback_items_failed INTEGER
            )
            """
        )
        self._ensure_fallback_audit_columns()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_log_created_at "
            "ON request_log(created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_log_application "
            "ON request_log(application, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_log_hostname "
            "ON request_log(hostname, created_at)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fallback_spend (
                day        TEXT    PRIMARY KEY,
                microusd   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()
        self._request_log_retention_days = request_log_retention_days
        self.reconcile_abandoned_requests()
        if request_log_retention_days:
            self.prune_request_log(request_log_retention_days)
        self._next_request_log_maintenance = (
            time.monotonic() + _REQUEST_LOG_MAINTENANCE_INTERVAL_SECONDS
        )
        if db_path != ":memory:":
            self._secure_database_files(path)

    @staticmethod
    def _secure_database_files(path: Path) -> None:
        """Restrict the database and any live WAL sidecars to the owner."""
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if candidate.exists():
                candidate.chmod(0o600)

    def _enable_wal(self) -> None:
        """Enable WAL with bounded retries for simultaneous process startup."""
        for attempt in range(8):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.05 * 2**attempt)

    def _ensure_fallback_audit_columns(self) -> None:
        """Add fallback audit columns to databases created by older releases."""
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(request_log)")
        }
        columns = {
            "fallback_used": "INTEGER NOT NULL DEFAULT 0",
            "fallback_provider": "TEXT",
            "fallback_cost_microusd": "INTEGER",
            "fallback_items_returned": "INTEGER",
            "fallback_items_failed": "INTEGER",
        }
        for name, declaration in columns.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE request_log ADD COLUMN {name} {declaration}"
                )

    # ── queries ────────────────────────────────────────────────

    @_locked
    def get_usage(self, key: str) -> int:
        """Return credits used by *key* in the current billing period."""
        row = self._conn.execute(
            "SELECT used FROM usage WHERE key_id = ? AND period = ?",
            (key, _current_period()),
        ).fetchone()
        return row[0] if row else 0

    @_locked
    def get_all_usage(self) -> dict[str, int]:
        """Return ``{key: used}`` for every key in the current period."""
        rows = self._conn.execute(
            "SELECT key_id, used FROM usage WHERE period = ?",
            (_current_period(),),
        ).fetchall()
        return {k: u for k, u in rows}

    @_locked
    def get_cooldown(self, key: str) -> int:
        """Return the unix timestamp until which *key* is in cooldown.

        Returns 0 if no cooldown is set. A returned value <= ``time.time()``
        means cooldown has expired and the key is eligible again.
        """
        row = self._conn.execute(
            "SELECT cooldown_until FROM cooldown WHERE key_id = ?",
            (key,),
        ).fetchone()
        return row[0] if row else 0

    @_locked
    def get_recent_requests(self, limit: int = 100) -> list[dict[str, object]]:
        """Return recent audit rows for diagnostics and tests.

        This is deliberately not an MCP tool because queries may contain
        sensitive business information.
        """
        bounded_limit = max(1, min(limit, 1000))
        cursor = self._conn.execute(
            "SELECT * FROM request_log ORDER BY id DESC LIMIT ?",
            (bounded_limit,),
        )
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    @_locked
    def get_fallback_request_summary(self) -> dict[str, object]:
        """Return completed fallback counts and reported costs for today/month."""
        def totals(modifier: str) -> dict[str, object]:
            row = self._conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(fallback_cost_microusd), 0)
                FROM request_log
                WHERE fallback_used = 1 AND status = 'succeeded'
                  AND datetime(created_at) >= datetime('now', ?)
                """,
                (modifier,),
            ).fetchone()
            endpoints = self._conn.execute(
                """
                SELECT endpoint, COUNT(*)
                FROM request_log
                WHERE fallback_used = 1 AND status = 'succeeded'
                  AND datetime(created_at) >= datetime('now', ?)
                GROUP BY endpoint
                """,
                (modifier,),
            ).fetchall()
            return {
                "completed": row[0],
                "cost_usd": row[1] / 1_000_000,
                "by_endpoint": {endpoint: count for endpoint, count in endpoints},
            }

        failed_today = self._conn.execute(
            """
            SELECT COUNT(*) FROM request_log
            WHERE fallback_used = 1 AND status = 'failed'
              AND datetime(created_at) >= datetime('now', 'start of day')
            """
        ).fetchone()[0]
        return {
            "today": totals("start of day"),
            "current_month": totals("start of month"),
            "failed_today": failed_today,
        }

    # ── mutations ──────────────────────────────────────────────

    @_locked
    def add_usage(self, key: str, credits: int) -> None:
        """Atomically add *credits* to *key* for the current period."""
        self._conn.execute(
            """
            INSERT INTO usage (key_id, period, used)
            VALUES (?, ?, ?)
            ON CONFLICT (key_id, period)
            DO UPDATE SET used = used + excluded.used
            """,
            (key, _current_period(), credits),
        )
        self._conn.commit()

    @_locked
    def set_cooldown(self, key: str, until_ts: int) -> None:
        """Set the cooldown expiry timestamp for *key*.

        ``until_ts`` is a unix timestamp. Any existing cooldown is replaced.
        """
        self._conn.execute(
            """
            INSERT INTO cooldown (key_id, cooldown_until)
            VALUES (?, ?)
            ON CONFLICT (key_id)
            DO UPDATE SET cooldown_until = excluded.cooldown_until
            """,
            (key, until_ts),
        )
        self._conn.commit()

    @_locked
    def start_request(
        self,
        *,
        endpoint: str,
        query: str | None,
        target: str | None,
        requester: Mapping[str, str | None],
    ) -> int:
        """Create an audit row before forwarding a request to Tavily."""
        self._maintain_request_log()
        cursor = self._conn.execute(
            """
            INSERT INTO request_log (
                created_at, endpoint, query, target, request_id, session_id,
                requester_id, hostname, application, application_version,
                source_ip, user_agent, transport
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                endpoint,
                query,
                target,
                requester.get("request_id"),
                requester.get("session_id"),
                requester.get("requester_id"),
                requester.get("hostname"),
                requester.get("application"),
                requester.get("application_version"),
                requester.get("source_ip"),
                requester.get("user_agent"),
                requester.get("transport"),
            ),
        )
        self._conn.commit()
        if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies it
            raise RuntimeError("SQLite did not return a request log row ID")
        return cursor.lastrowid

    def _maintain_request_log(self) -> None:
        """Periodically reconcile and prune audit rows during long uptimes."""
        now = time.monotonic()
        if now < self._next_request_log_maintenance:
            return
        self.reconcile_abandoned_requests()
        if self._request_log_retention_days:
            self.prune_request_log(self._request_log_retention_days)
        self._next_request_log_maintenance = (
            now + _REQUEST_LOG_MAINTENANCE_INTERVAL_SECONDS
        )

    @_locked
    def finish_request(
        self,
        request_log_id: int,
        *,
        status: str,
        attempts: int,
        duration_ms: int,
        credits: int | None = None,
        error_code: str | None = None,
        fallback: Mapping[str, object] | None = None,
    ) -> None:
        """Complete an audit row with outcome data."""
        if status not in {"succeeded", "failed", "cancelled", "abandoned"}:
            raise ValueError(
                "status must be 'succeeded', 'failed', 'cancelled' or 'abandoned'"
            )
        fallback = fallback or {}
        cost_usd = fallback.get("cost_usd")
        cost_microusd = (
            math.ceil(float(cost_usd) * 1_000_000)
            if isinstance(cost_usd, (int, float))
            else None
        )
        self._conn.execute(
            """
            UPDATE request_log
            SET completed_at = ?, status = ?, credits = ?, attempts = ?,
                duration_ms = ?, error_code = ?, fallback_used = ?,
                fallback_provider = ?, fallback_cost_microusd = ?,
                fallback_items_returned = ?, fallback_items_failed = ?
            WHERE id = ?
            """,
            (
                _utc_now(),
                status,
                credits,
                attempts,
                duration_ms,
                error_code,
                int(bool(fallback)),
                fallback.get("provider"),
                cost_microusd,
                fallback.get("items_returned"),
                fallback.get("items_failed"),
                request_log_id,
            ),
        )
        self._conn.commit()

    @_locked
    def reconcile_abandoned_requests(self, older_than_hours: int = 24) -> int:
        """Mark stale started rows abandoned after an abrupt process exit."""
        if older_than_hours < 1:
            raise ValueError("older_than_hours must be at least 1")
        cursor = self._conn.execute(
            """
            UPDATE request_log
            SET completed_at = ?, status = 'abandoned', error_code = 'process_restart'
            WHERE status = 'started'
              AND julianday(created_at) < julianday('now', ?)
            """,
            (_utc_now(), f"-{older_than_hours} hours"),
        )
        self._conn.commit()
        return cursor.rowcount

    @_locked
    def prune_request_log(self, retention_days: int) -> int:
        """Delete completed audit rows older than the configured retention."""
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        cursor = self._conn.execute(
            """
            DELETE FROM request_log
            WHERE status != 'started'
              AND julianday(created_at) < julianday('now', ?)
            """,
            (f"-{retention_days} days",),
        )
        self._conn.commit()
        return cursor.rowcount

    @_locked
    def reserve_fallback_cost(
        self, amount_usd: float, daily_limit_usd: float
    ) -> str | None:
        """Atomically reserve budget and return its UTC day, or ``None``."""
        amount = math.ceil(amount_usd * 1_000_000)
        limit = math.floor(daily_limit_usd * 1_000_000)
        day = _current_day()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT microusd FROM fallback_spend WHERE day = ?",
                (day,),
            ).fetchone()
            used = row[0] if row else 0
            if used + amount > limit:
                self._conn.rollback()
                return None
            self._conn.execute(
                """
                INSERT INTO fallback_spend (day, microusd) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET microusd = microusd + excluded.microusd
                """,
                (day, amount),
            )
            self._conn.commit()
            return day
        except Exception:
            self._conn.rollback()
            raise

    def settle_fallback_cost(
        self, reservation_day: str, reserved_usd: float, actual_usd: float
    ) -> None:
        """Replace a reservation with the provider-reported actual cost."""
        reserved = math.ceil(reserved_usd * 1_000_000)
        actual = math.ceil(actual_usd * 1_000_000)
        self._conn.execute(
            """
            UPDATE fallback_spend
            SET microusd = MAX(0, microusd - ? + ?)
            WHERE day = ?
            """,
            (reserved, actual, reservation_day),
        )
        self._conn.commit()

    def get_fallback_spend(self) -> float:
        """Return today's reserved and settled fallback spend in USD."""
        row = self._conn.execute(
            "SELECT microusd FROM fallback_spend WHERE day = ?",
            (_current_day(),),
        ).fetchone()
        return (row[0] if row else 0) / 1_000_000

    @_locked
    def close(self) -> None:
        self._conn.close()


# ── credit estimation ──────────────────────────────────────────


def estimate_credits(endpoint: str, params: dict) -> int:
    """Estimate the credit cost of a Tavily API request.

    This is used for routing before the request. The actual ``usage.credits``
    value from a successful response takes precedence.
    """
    match endpoint:
        case "search":
            return 2 if params.get("search_depth") == "advanced" else 1

        case "extract":
            urls = params.get("urls", [])
            url_count = len(urls) if isinstance(urls, list) else 1
            multiplier = 2 if params.get("extract_depth") == "advanced" else 1
            return max(1, math.ceil(url_count / 5) * multiplier)

        case "map":
            base = 2 if params.get("instructions") else 1
            pages = params.get("limit", 50)
            return max(1, math.ceil(pages / 10) * base)

        case "crawl":
            multiplier = 2 if params.get("extract_depth") == "advanced" else 1
            pages = params.get("limit", 50)
            return max(1, math.ceil(pages / 5) * multiplier)

        case _:
            return 1
