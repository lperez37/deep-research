"""SQLite-backed credit tracker with automatic monthly reset."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _current_period() -> str:
    """Return current billing period as 'YYYY-MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class CreditTracker:
    """Tracks per-key credit usage in a SQLite database.

    Thread-safe via SQLite's built-in locking.  Each mutation is a single
    atomic statement so concurrent asyncio tasks are safe when wrapped
    with ``asyncio.Lock`` at the router level.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        path = Path(db_path)
        if db_path != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
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
        self._conn.commit()

    # ── queries ────────────────────────────────────────────────

    def get_usage(self, key: str) -> int:
        """Return credits used by *key* in the current billing period."""
        row = self._conn.execute(
            "SELECT used FROM usage WHERE key_id = ? AND period = ?",
            (key, _current_period()),
        ).fetchone()
        return row[0] if row else 0

    def get_all_usage(self) -> dict[str, int]:
        """Return ``{key: used}`` for every key in the current period."""
        rows = self._conn.execute(
            "SELECT key_id, used FROM usage WHERE period = ?",
            (_current_period(),),
        ).fetchall()
        return {k: u for k, u in rows}

    # ── mutations ──────────────────────────────────────────────

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

    def close(self) -> None:
        self._conn.close()


# ── credit estimation ──────────────────────────────────────────


def estimate_credits(endpoint: str, params: dict) -> int:
    """Estimate the credit cost of a Tavily API request.

    Used for routing decisions *before* sending the request.
    After the request completes, the actual ``usage.credits`` value
    from the response takes precedence.
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

        case "research":
            model = params.get("model", "auto")
            if model == "pro":
                return 60
            if model == "mini":
                return 30
            return 45  # "auto" — use midpoint

        case _:
            return 1
