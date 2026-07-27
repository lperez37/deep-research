"""Round-robin key router with credit-aware skipping and 429 cooldown."""

from __future__ import annotations

import asyncio
import time

from deep_research.credits import CreditTracker


class KeyRouter:
    """Selects the next available Tavily API key.

    Uses round-robin rotation and skips any key that is either:
      - over its monthly credit budget (local accounting), or
      - in cooldown after a recent Tavily 429 (independent of local accounting).

    The cooldown layer protects against drift between the local credit counter
    and Tavily's authoritative meter: if a key 429s while we still believe it
    has budget left, we mark it cool for ``cooldown_hours`` and probe again
    after it expires. The probe survives monthly rollover, so a key that
    Tavily hasn't actually reset yet doesn't get permanently locked out for
    the new period after a single stale 429.
    """

    def __init__(
        self,
        keys: list[str],
        credits_per_key: int,
        tracker: CreditTracker,
        cooldown_hours: float = 24.0,
    ) -> None:
        if not keys:
            raise ValueError("At least one API key is required")
        self._keys = list(keys)
        self._credits_per_key = credits_per_key
        self._tracker = tracker
        self._cooldown_seconds = int(cooldown_hours * 3600)
        self._index = 0
        self._lock = asyncio.Lock()

    @property
    def key_count(self) -> int:
        return len(self._keys)

    async def get_key(self) -> str:
        """Return the next key that has budget AND is not in cooldown."""
        async with self._lock:
            now = int(time.time())
            for _ in range(len(self._keys)):
                key = self._keys[self._index]
                self._index = (self._index + 1) % len(self._keys)
                used, cooldown_until = await asyncio.to_thread(
                    lambda key=key: (
                        self._tracker.get_usage(key),
                        self._tracker.get_cooldown(key),
                    )
                )
                if used < self._credits_per_key and cooldown_until <= now:
                    return key
            raise RuntimeError("All API keys are exhausted or in cooldown")

    async def report_usage(self, key: str, credits: int) -> None:
        """Record credit consumption for a key."""
        await asyncio.to_thread(self._tracker.add_usage, key, credits)

    async def mark_rate_limited(self, key: str) -> None:
        """Mark *key* as cooled-down after a Tavily 429.

        The key is skipped for the next ``cooldown_hours``. After that, the
        router probes it again on the next ``get_key`` call. If the probe
        succeeds, normal rotation resumes; if it 429s again, this method is
        called again and cooldown re-extends.
        """
        until_ts = int(time.time()) + self._cooldown_seconds
        await asyncio.to_thread(self._tracker.set_cooldown, key, until_ts)

    async def get_status_async(self) -> list[dict]:
        """Return status without blocking the event loop on SQLite."""
        return await asyncio.to_thread(self.get_status)

    def get_status(self) -> list[dict]:
        """Return credit + cooldown status for every key (for the status tool)."""
        now = int(time.time())
        result = []
        for k in self._keys:
            used = self._tracker.get_usage(k)
            remaining = max(0, self._credits_per_key - used)
            utilization = (
                round(used / self._credits_per_key * 100, 1)
                if self._credits_per_key > 0
                else 0
            )
            cooldown_until = self._tracker.get_cooldown(k)
            in_cooldown = cooldown_until > now
            result.append(
                {
                    "key": f"{k[:8]}...{k[-4:]}",
                    "used": used,
                    "limit": self._credits_per_key,
                    "remaining": remaining,
                    "utilization_pct": utilization,
                    "in_cooldown": in_cooldown,
                    "cooldown_until": cooldown_until if in_cooldown else 0,
                }
            )
        return result
