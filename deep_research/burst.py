"""In-memory per-session burst protection for metered Tavily calls."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from threading import Lock


class BurstLimitExceeded(RuntimeError):
    """Raised before an upstream call would exceed the session burst budget."""


class SessionBurstLimiter:
    """Bound metered calls per MCP session in a rolling time window."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_sessions: int = 10_000,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._max_sessions = max_sessions
        self._requests: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def check(self, session_id: str | None) -> None:
        """Record an allowed call or raise when the rolling budget is exhausted."""
        if self.limit == 0 or session_id is None:
            return

        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests.get(session_id)
            if requests is None:
                if len(self._requests) >= self._max_sessions:
                    self._requests.popitem(last=False)
                requests = deque()
                self._requests[session_id] = requests
            else:
                self._requests.move_to_end(session_id)
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.limit:
                retry_after = max(1, int(requests[0] + self.window_seconds - now + 0.999))
                raise BurstLimitExceeded(
                    f"Tavily session burst limit reached ({self.limit} calls per "
                    f"{self.window_seconds:g}s); retry in {retry_after}s"
                )
            requests.append(now)
