"""Round-robin key router with credit-aware skipping."""

from __future__ import annotations

import asyncio

from deep_research.credits import CreditTracker


class KeyRouter:
    """Selects the next available Tavily API key.

    Uses round-robin rotation and skips any key whose estimated usage
    has reached the monthly credit limit.
    """

    def __init__(
        self,
        keys: list[str],
        credits_per_key: int,
        tracker: CreditTracker,
    ) -> None:
        if not keys:
            raise ValueError("At least one API key is required")
        self._keys = list(keys)
        self._credits_per_key = credits_per_key
        self._tracker = tracker
        self._index = 0
        self._lock = asyncio.Lock()

    @property
    def key_count(self) -> int:
        return len(self._keys)

    async def get_key(self) -> str:
        """Return the next key that has remaining credit budget."""
        async with self._lock:
            for _ in range(len(self._keys)):
                key = self._keys[self._index]
                self._index = (self._index + 1) % len(self._keys)
                used = self._tracker.get_usage(key)
                if used < self._credits_per_key:
                    return key
            raise RuntimeError(
                "All API keys have exhausted their monthly credit budget"
            )

    async def report_usage(self, key: str, credits: int) -> None:
        """Record credit consumption for a key."""
        self._tracker.add_usage(key, credits)

    async def force_exhaust(self, key: str) -> None:
        """Mark a key as fully exhausted (e.g. after a 429 response)."""
        remaining = self._credits_per_key - self._tracker.get_usage(key)
        if remaining > 0:
            self._tracker.add_usage(key, remaining)

    def get_status(self) -> list[dict]:
        """Return credit status for every key (for the status tool)."""
        result = []
        for k in self._keys:
            used = self._tracker.get_usage(k)
            remaining = max(0, self._credits_per_key - used)
            utilization = round(used / self._credits_per_key * 100, 1) if self._credits_per_key > 0 else 0
            result.append({
                "key": f"{k[:8]}...{k[-4:]}",
                "used": used,
                "limit": self._credits_per_key,
                "remaining": remaining,
                "utilization_pct": utilization,
            })
        return result
