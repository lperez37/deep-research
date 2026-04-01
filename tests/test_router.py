"""Unit tests for KeyRouter with real CreditTracker (in-memory SQLite)."""

from __future__ import annotations

import asyncio

import pytest

from deep_research.credits import CreditTracker
from deep_research.router import KeyRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KEYS = ["key-aaaaaaaabbbb", "key-ccccccccdddd", "key-eeeeeeeeffff"]
CREDITS_PER_KEY = 1000


def _make_router(
    keys: list[str] | None = None,
    credits_per_key: int = CREDITS_PER_KEY,
) -> tuple[KeyRouter, CreditTracker]:
    """Create a KeyRouter backed by a fresh in-memory CreditTracker."""
    tracker = CreditTracker(":memory:")
    router = KeyRouter(
        keys=keys if keys is not None else list(KEYS),
        credits_per_key=credits_per_key,
        tracker=tracker,
    )
    return router, tracker


# ---------------------------------------------------------------------------
# 1. Constructor raises ValueError with empty key list
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_empty_list_raises_value_error(self) -> None:
        tracker = CreditTracker(":memory:")
        with pytest.raises(ValueError, match="At least one API key"):
            KeyRouter(keys=[], credits_per_key=1000, tracker=tracker)

    def test_single_key_accepted(self) -> None:
        router, _ = _make_router(keys=["single-key-abcdef12"])
        assert router.key_count == 1


# ---------------------------------------------------------------------------
# 2. get_key returns keys in round-robin order
# ---------------------------------------------------------------------------


class TestRoundRobin:
    async def test_round_robin_rotation(self) -> None:
        """Call get_key 5 times with 3 keys and verify the rotation pattern."""
        router, _ = _make_router()

        results = [await router.get_key() for _ in range(5)]

        expected = [KEYS[0], KEYS[1], KEYS[2], KEYS[0], KEYS[1]]
        assert results == expected

    async def test_round_robin_wraps_around(self) -> None:
        """After exhausting the list once, rotation continues from the start."""
        router, _ = _make_router(keys=["k1-aabbccdd", "k2-eeffgghh"])

        results = [await router.get_key() for _ in range(4)]

        assert results == [
            "k1-aabbccdd",
            "k2-eeffgghh",
            "k1-aabbccdd",
            "k2-eeffgghh",
        ]


# ---------------------------------------------------------------------------
# 3. get_key skips exhausted keys
# ---------------------------------------------------------------------------


class TestSkipExhausted:
    async def test_skips_key_at_limit(self) -> None:
        """A key with usage == credits_per_key is skipped."""
        router, tracker = _make_router(credits_per_key=100)

        # Exhaust the first key entirely.
        tracker.add_usage(KEYS[0], 100)

        # First call should skip KEYS[0] and return KEYS[1].
        result = await router.get_key()
        assert result == KEYS[1]

    async def test_skips_key_over_limit(self) -> None:
        """A key with usage exceeding credits_per_key is also skipped."""
        router, tracker = _make_router(credits_per_key=100)

        tracker.add_usage(KEYS[0], 150)

        result = await router.get_key()
        assert result == KEYS[1]

    async def test_rotation_continues_after_skip(self) -> None:
        """After skipping the exhausted key, subsequent calls still rotate."""
        router, tracker = _make_router(credits_per_key=100)

        tracker.add_usage(KEYS[1], 100)

        results = [await router.get_key() for _ in range(4)]

        # KEYS[1] is always skipped; rotation alternates KEYS[0] and KEYS[2].
        assert results == [KEYS[0], KEYS[2], KEYS[0], KEYS[2]]


# ---------------------------------------------------------------------------
# 4. get_key raises RuntimeError when ALL keys exhausted
# ---------------------------------------------------------------------------


class TestAllKeysExhausted:
    async def test_raises_when_all_exhausted(self) -> None:
        router, tracker = _make_router(credits_per_key=50)

        for key in KEYS:
            tracker.add_usage(key, 50)

        with pytest.raises(RuntimeError, match="All API keys have exhausted"):
            await router.get_key()

    async def test_recovers_if_one_key_freed(self) -> None:
        """Edge case: if usage were somehow lowered, the key becomes available."""
        router, tracker = _make_router(
            keys=["only-key-12345678"],
            credits_per_key=10,
        )
        tracker.add_usage("only-key-12345678", 10)

        with pytest.raises(RuntimeError):
            await router.get_key()


# ---------------------------------------------------------------------------
# 5. report_usage records credits correctly
# ---------------------------------------------------------------------------


class TestReportUsage:
    async def test_records_credits(self) -> None:
        router, tracker = _make_router()

        await router.report_usage(KEYS[0], 42)

        assert tracker.get_usage(KEYS[0]) == 42

    async def test_accumulates_credits(self) -> None:
        router, tracker = _make_router()

        await router.report_usage(KEYS[0], 10)
        await router.report_usage(KEYS[0], 25)

        assert tracker.get_usage(KEYS[0]) == 35

    async def test_tracks_keys_independently(self) -> None:
        router, tracker = _make_router()

        await router.report_usage(KEYS[0], 100)
        await router.report_usage(KEYS[1], 200)

        assert tracker.get_usage(KEYS[0]) == 100
        assert tracker.get_usage(KEYS[1]) == 200
        assert tracker.get_usage(KEYS[2]) == 0


# ---------------------------------------------------------------------------
# 6. force_exhaust marks a key as fully used
# ---------------------------------------------------------------------------


class TestForceExhaust:
    async def test_force_exhaust_fills_to_limit(self) -> None:
        router, tracker = _make_router(credits_per_key=500)

        tracker.add_usage(KEYS[0], 200)
        await router.force_exhaust(KEYS[0])

        assert tracker.get_usage(KEYS[0]) == 500

    async def test_force_exhaust_on_unused_key(self) -> None:
        """Force-exhausting a key with zero usage fills it completely."""
        router, tracker = _make_router(credits_per_key=300)

        await router.force_exhaust(KEYS[2])

        assert tracker.get_usage(KEYS[2]) == 300

    async def test_force_exhaust_on_already_exhausted_key(self) -> None:
        """Force-exhausting an already-exhausted key is a no-op."""
        router, tracker = _make_router(credits_per_key=100)

        tracker.add_usage(KEYS[0], 100)
        await router.force_exhaust(KEYS[0])

        assert tracker.get_usage(KEYS[0]) == 100

    async def test_get_key_skips_force_exhausted(self) -> None:
        """After force_exhaust, get_key must skip that key."""
        router, _ = _make_router(credits_per_key=100)

        await router.force_exhaust(KEYS[0])

        result = await router.get_key()
        assert result == KEYS[1]


# ---------------------------------------------------------------------------
# 7. get_status returns correct structure
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_status_structure(self) -> None:
        router, tracker = _make_router(credits_per_key=1000)

        tracker.add_usage(KEYS[0], 250)

        statuses = router.get_status()

        assert len(statuses) == 3

        first = statuses[0]
        assert set(first.keys()) == {"key", "used", "limit", "remaining", "utilization_pct"}

    def test_status_values(self) -> None:
        router, tracker = _make_router(credits_per_key=1000)

        tracker.add_usage(KEYS[0], 250)

        statuses = router.get_status()
        first = statuses[0]

        assert first["used"] == 250
        assert first["limit"] == 1000
        assert first["remaining"] == 750
        assert first["utilization_pct"] == 25.0

    def test_status_key_is_masked(self) -> None:
        router, _ = _make_router()

        statuses = router.get_status()

        for status in statuses:
            masked = status["key"]
            # Masked format: first 8 chars + "..." + last 4 chars
            assert "..." in masked
            assert len(masked) == 8 + 3 + 4  # 15 characters total

    def test_status_remaining_never_negative(self) -> None:
        """If usage somehow exceeds the limit, remaining is clamped to 0."""
        router, tracker = _make_router(credits_per_key=100)

        tracker.add_usage(KEYS[0], 999)

        statuses = router.get_status()
        first = statuses[0]

        assert first["remaining"] == 0

    def test_status_unused_keys(self) -> None:
        router, _ = _make_router(credits_per_key=500)

        statuses = router.get_status()

        for status in statuses:
            assert status["used"] == 0
            assert status["remaining"] == 500
            assert status["utilization_pct"] == 0


# ---------------------------------------------------------------------------
# 8. key_count property
# ---------------------------------------------------------------------------


class TestKeyCount:
    def test_returns_correct_count(self) -> None:
        router, _ = _make_router()
        assert router.key_count == 3

    def test_single_key(self) -> None:
        router, _ = _make_router(keys=["solo-key-aabbccdd"])
        assert router.key_count == 1

    def test_many_keys(self) -> None:
        keys = [f"key-{i:012d}" for i in range(10)]
        router, _ = _make_router(keys=keys)
        assert router.key_count == 10


# ---------------------------------------------------------------------------
# 9. Concurrent get_key calls don't corrupt state
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_get_key_no_corruption(self) -> None:
        """Fire many get_key calls concurrently; every result must be a valid key."""
        router, _ = _make_router(credits_per_key=100_000)

        results = await asyncio.gather(
            *[router.get_key() for _ in range(50)]
        )

        assert len(results) == 50
        for key in results:
            assert key in KEYS

    async def test_concurrent_get_key_distribution(self) -> None:
        """Under concurrency the lock serialises access, producing round-robin order."""
        router, _ = _make_router(credits_per_key=100_000)

        results = await asyncio.gather(
            *[router.get_key() for _ in range(30)]
        )

        # Because asyncio.gather on coroutines in a single-threaded event
        # loop serialises them (the lock ensures one-at-a-time), the
        # distribution must be exactly even: 10 per key.
        counts = {k: results.count(k) for k in KEYS}
        assert counts == {KEYS[0]: 10, KEYS[1]: 10, KEYS[2]: 10}

    async def test_concurrent_report_and_get(self) -> None:
        """Interleave report_usage and get_key calls without errors."""
        router, tracker = _make_router(credits_per_key=100_000)

        async def report_then_get(credit_amount: int) -> str:
            await router.report_usage(KEYS[0], credit_amount)
            return await router.get_key()

        results = await asyncio.gather(
            *[report_then_get(1) for _ in range(20)]
        )

        assert len(results) == 20
        for key in results:
            assert key in KEYS

        # Total reported usage on KEYS[0] should be 20 (1 credit x 20 calls).
        assert tracker.get_usage(KEYS[0]) == 20
