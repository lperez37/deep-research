"""Integration tests for audited MCP tool routing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time

import pytest
from fastmcp import Client
from mcp.types import Implementation

_SERVER_ENV = {
    "TAVILY_API_KEYS": "test-key-12345678",
    "DB_PATH": ":memory:",
}
_ORIGINAL_SERVER_ENV = {name: os.environ.get(name) for name in _SERVER_ENV}
os.environ.update(_SERVER_ENV)
try:
    from deep_research import server
finally:
    for name, value in _ORIGINAL_SERVER_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

from deep_research.credits import CreditTracker
from deep_research.router import KeyRouter
from deep_research.tavily_client import TavilyAPIError


class SuccessfulClient:
    async def request(self, endpoint: str, key: str, params: dict) -> dict:
        assert endpoint == "search"
        assert key == "test-key-12345678"
        assert params["query"] == "audit this query"
        assert "ctx" not in params
        return {"results": [], "usage": {"credits": 2}}


class FailingClient:
    async def request(self, endpoint: str, key: str, params: dict) -> dict:
        raise RuntimeError("upstream unavailable with private detail")


class RateLimitedThenSuccessfulClient:
    def __init__(self) -> None:
        self.calls = 0

    async def request(self, endpoint: str, key: str, params: dict) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise TavilyAPIError(429, "quota exceeded")
        return {"results": [], "usage": {"credits": 1}}


class AlwaysRateLimitedClient:
    def __init__(self) -> None:
        self.calls = 0

    async def request(self, endpoint: str, key: str, params: dict) -> dict:
        self.calls += 1
        raise TavilyAPIError(429, "quota exceeded")


class GenericSuccessfulClient:
    async def request(self, endpoint: str, key: str, params: dict) -> dict:
        return {"endpoint": endpoint, "usage": {"credits": 1}}


class CancellableClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def request(self, endpoint: str, key: str, params: dict) -> dict:
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


async def test_complete_public_tool_schemas_match_main_snapshot() -> None:
    async with Client(server.mcp, name="schema-tester") as client:
        tools = await client.list_tools()

    serialized = sorted(
        [
            tool.model_dump(mode="json", by_alias=True, exclude_none=False)
            for tool in tools
        ],
        key=lambda item: item["name"],
    )
    payload = json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()

    # Canonical complete five-tool snapshot from main under FastMCP 3.4.5.
    assert hashlib.sha256(payload).hexdigest() == (
        "478380324ef33333238e81576840e20237c89fced061b66ff9c134ab64905c3a"
    )
    assert all(
        "ctx" not in tool["inputSchema"].get("properties", {}) for tool in serialized
    )


async def _install_test_dependencies(monkeypatch, *, keys=None):
    tracker = CreditTracker(":memory:")
    router = KeyRouter(
        keys=keys or ["test-key-12345678"],
        credits_per_key=1000,
        tracker=tracker,
    )
    monkeypatch.setattr(server, "tracker", tracker)
    monkeypatch.setattr(server, "audit_tracker", tracker)
    monkeypatch.setattr(server, "router", router)
    return tracker


async def test_search_call_is_logged_end_to_end(monkeypatch) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    monkeypatch.setattr(server, "client", SuccessfulClient())

    try:
        async with Client(
            server.mcp,
            client_info=Implementation(name="adversarial-reviewer", version="9.1"),
        ) as client:
            result = await client.call_tool(
                "tavily-search",
                {"query": "audit this query", "search_depth": "advanced"},
            )

        assert result.data["results"] == []
        row = tracker.get_recent_requests()[0]
        assert row["endpoint"] == "search"
        assert row["query"] == "audit this query"
        assert row["application"] == "adversarial-reviewer"
        assert row["application_version"] == "9.1"
        assert row["status"] == "succeeded"
        assert row["credits"] == 2
        assert row["attempts"] == 1
        assert row["request_id"] is not None
        assert row["session_id"] is not None
    finally:
        tracker.close()


async def test_failed_call_logs_code_not_exception_message(monkeypatch) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    monkeypatch.setattr(server, "client", FailingClient())

    try:
        async with Client(server.mcp, name="failure-tester") as client:
            result = await client.call_tool(
                "tavily-search",
                {"query": "failed query"},
                raise_on_error=False,
            )

        assert result.is_error

        row = tracker.get_recent_requests()[0]
        assert row["query"] == "failed query"
        assert row["status"] == "failed"
        assert row["error_code"] == "RuntimeError"
        assert "private detail" not in str(row)
    finally:
        tracker.close()


async def test_rate_limit_retry_updates_one_audit_row(monkeypatch) -> None:
    tracker = await _install_test_dependencies(
        monkeypatch,
        keys=["test-key-12345678", "second-key-123456"],
    )
    fake_client = RateLimitedThenSuccessfulClient()
    monkeypatch.setattr(server, "client", fake_client)

    try:
        async with Client(server.mcp, name="retry-tester") as client:
            result = await client.call_tool("tavily-search", {"query": "retry query"})

        assert result.data["results"] == []
        rows = tracker.get_recent_requests()
        assert len(rows) == 1
        assert rows[0]["status"] == "succeeded"
        assert rows[0]["attempts"] == 2
        assert rows[0]["credits"] == 1
        assert fake_client.calls == 2
    finally:
        tracker.close()


async def test_terminal_rate_limit_cools_key_and_preserves_attempt(monkeypatch) -> None:
    key = "test-key-12345678"
    tracker = await _install_test_dependencies(monkeypatch, keys=[key])
    fake_client = AlwaysRateLimitedClient()
    monkeypatch.setattr(server, "client", fake_client)

    try:
        async with Client(server.mcp, name="rate-limit-tester") as client:
            result = await client.call_tool(
                "tavily-search", {"query": "limited"}, raise_on_error=False
            )

        assert result.is_error
        row = tracker.get_recent_requests()[0]
        assert row["status"] == "failed"
        assert row["attempts"] == 1
        assert row["error_code"] == "tavily_http_429"
        assert fake_client.calls == 1
        assert tracker.get_cooldown(key) > 0
    finally:
        tracker.close()


async def test_no_available_key_records_zero_attempts(monkeypatch) -> None:
    key = "test-key-12345678"
    tracker = await _install_test_dependencies(monkeypatch, keys=[key])
    tracker.add_usage(key, 1000)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())

    try:
        async with Client(server.mcp, name="exhausted-key-tester") as client:
            result = await client.call_tool(
                "tavily-search", {"query": "no capacity"}, raise_on_error=False
            )

        assert result.is_error
        row = tracker.get_recent_requests()[0]
        assert row["status"] == "failed"
        assert row["attempts"] == 0
        assert row["error_code"] == "RuntimeError"
    finally:
        tracker.close()


@pytest.mark.parametrize(
    ("tool_name", "arguments", "endpoint", "query", "target"),
    [
        ("tavily-search", {"query": "search q"}, "search", "search q", None),
        (
            "tavily-extract",
            {
                "urls": [
                    "https://user:pass@example.com/a?token=secret#fragment",
                    "https://example.org/b?api_key=hidden",
                ],
                "query": "rerank q",
            },
            "extract",
            "rerank q",
            '["https://example.com/[path-sha256:6a50dc8584134c7d]","https://example.org/[path-sha256:9812b0b9d61e09b9]"]',
        ),
        (
            "tavily-crawl",
            {
                "url": "https://example.com/docs?token=secret",
                "instructions": "find API docs",
            },
            "crawl",
            "find API docs",
            "https://example.com/[path-sha256:a2557b8d5a9637c8]",
        ),
        (
            "tavily-map",
            {
                "url": "https://example.com/#private",
                "instructions": "map docs",
            },
            "map",
            "map docs",
            "https://example.com",
        ),
    ],
)
async def test_every_tavily_tool_records_sanitised_audit_data(
    monkeypatch, tool_name, arguments, endpoint, query, target
) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())

    try:
        async with Client(server.mcp, name="all-tools-tester") as client:
            result = await client.call_tool(tool_name, arguments)

        assert result.data["endpoint"] == endpoint
        row = tracker.get_recent_requests()[0]
        assert row["endpoint"] == endpoint
        assert row["query"] == query
        assert row["target"] == target
        assert "secret" not in str(row["target"])
        assert "pass" not in str(row["target"])
    finally:
        tracker.close()


@pytest.mark.parametrize(
    "malformed",
    [
        "//user:password@example.com/a?token=secret#fragment",
        "https:////user:password@example.com/a?token=secret#fragment",
        "not-a-url?token=secret",
        "https://example.com\\reset\\SECRET-PATH?token=secret",
        "https://example.com%2freset%2fSECRET-PATH?token=secret",
        "ftp://example.com/secret",
        f"https://{'a' * 1_000_000}/secret",
    ],
)
def test_malformed_url_never_preserves_secret_locations(malformed: str) -> None:
    assert server._sanitize_url(malformed) == "[invalid URL]"


def test_url_paths_are_fingerprinted_not_persisted() -> None:
    sanitized = server._sanitize_url("https://example.com/reset/SECRET-TOKEN")
    assert sanitized.startswith("https://example.com/[path-sha256:")
    assert "SECRET" not in sanitized
    assert "/reset/" not in sanitized


def test_url_target_list_is_bounded() -> None:
    target = server._audit_target(
        {"urls": [f"https://host{i}.example/path" for i in range(1_000)]}
    )
    assert isinstance(target, str)
    assert len(target) == server.settings.audit_max_text_chars
    assert "[truncated sha256:" in target


async def test_oversized_query_is_bounded_in_audit_copy(monkeypatch) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())
    query = "q" * 20_000 + "PRIVATE-END"

    try:
        async with Client(server.mcp, name="large-query-tester") as client:
            await client.call_tool("tavily-search", {"query": query})

        stored = tracker.get_recent_requests()[0]["query"]
        assert isinstance(stored, str)
        assert len(stored) == server.settings.audit_max_text_chars
        assert stored.endswith("]")
        assert "[truncated sha256:" in stored
        assert "PRIVATE-END" not in stored
    finally:
        tracker.close()


async def test_audit_lock_contention_is_bounded_and_fails_open(
    monkeypatch, tmp_path
) -> None:
    credit_tracker = CreditTracker(":memory:")
    audit_tracker = CreditTracker(
        str(tmp_path / "audit.db"),
        busy_timeout_seconds=0.05,
        request_log_retention_days=0,
    )
    router = KeyRouter(
        keys=["test-key-12345678"],
        credits_per_key=100,
        tracker=credit_tracker,
    )
    monkeypatch.setattr(server, "tracker", credit_tracker)
    monkeypatch.setattr(server, "audit_tracker", audit_tracker)
    monkeypatch.setattr(server, "router", router)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())

    blocker = sqlite3.connect(tmp_path / "audit.db", timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        async with Client(server.mcp, name="contention-tester") as client:
            result = await client.call_tool("tavily-search", {"query": "still runs"})
        assert result.is_error is False
        assert time.monotonic() - started < 0.5
    finally:
        blocker.rollback()
        blocker.close()
        audit_tracker.close()
        credit_tracker.close()


async def test_cancelled_request_is_completed_as_cancelled(monkeypatch) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    cancellable = CancellableClient()
    monkeypatch.setattr(server, "client", cancellable)

    try:
        async with Client(server.mcp, name="cancellation-tester") as client:
            call = asyncio.create_task(
                client.call_tool("tavily-search", {"query": "cancel me"})
            )
            await asyncio.wait_for(cancellable.started.wait(), timeout=1)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call

        row = tracker.get_recent_requests()[0]
        for _ in range(100):
            if row["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
            row = tracker.get_recent_requests()[0]
        assert row["status"] == "cancelled"
        assert row["completed_at"] is not None
        assert row["error_code"] == "CancelledError"
    finally:
        tracker.close()


async def test_cancellation_after_upstream_success_keeps_success_status(
    monkeypatch,
) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())
    reporting_started = asyncio.Event()

    async def blocked_report_usage(key: str, credits: int) -> None:
        reporting_started.set()
        await asyncio.Future()

    monkeypatch.setattr(server.router, "report_usage", blocked_report_usage)

    try:
        async with Client(server.mcp, name="post-success-cancellation-tester") as client:
            call = asyncio.create_task(
                client.call_tool("tavily-search", {"query": "completed upstream"})
            )
            await asyncio.wait_for(reporting_started.wait(), timeout=1)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call

        row = tracker.get_recent_requests()[0]
        for _ in range(100):
            if row["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)
            row = tracker.get_recent_requests()[0]
        assert row["status"] == "succeeded"
        assert row["credits"] == 1
        assert row["attempts"] == 1
        assert row["error_code"] is None
    finally:
        tracker.close()


async def test_malformed_audit_text_does_not_block_upstream(monkeypatch) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())
    query = "q" * server.settings.audit_max_text_chars + "\ud800"

    try:
        async with Client(server.mcp, name="malformed-audit-tester") as client:
            result = await client.call_tool("tavily-search", {"query": query})

        assert result.data["endpoint"] == "search"
        assert tracker.get_recent_requests() == []
    finally:
        tracker.close()


async def test_repeated_cancellation_cleans_row_committed_before_id_return(
    monkeypatch,
) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    committed = asyncio.Event()
    release_id = asyncio.Event()

    async def delayed_start(endpoint: str, params: dict, ctx: object) -> int:
        request_id = tracker.start_request(
            endpoint=endpoint,
            query=str(params["query"]),
            target=None,
            requester={},
        )
        committed.set()
        await release_id.wait()
        return request_id

    monkeypatch.setattr(server, "_start_audit", delayed_start)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())
    call = asyncio.create_task(
        server._route_request("search", {"query": "cancel before row ID"}, object())
    )

    try:
        await asyncio.wait_for(committed.wait(), timeout=1)
        call.cancel()
        await asyncio.sleep(0)
        call.cancel()
        release_id.set()
        with pytest.raises(asyncio.CancelledError):
            await call

        row = tracker.get_recent_requests()[0]
        for _ in range(100):
            if row["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
            row = tracker.get_recent_requests()[0]
        assert row["status"] == "cancelled"
        assert row["completed_at"] is not None
    finally:
        release_id.set()
        tracker.close()


async def test_failure_after_upstream_success_preserves_consumed_credits(
    monkeypatch,
) -> None:
    tracker = await _install_test_dependencies(monkeypatch)
    monkeypatch.setattr(server, "client", GenericSuccessfulClient())
    monkeypatch.setattr(
        server,
        "_credits_summary",
        lambda: (_ for _ in ()).throw(RuntimeError("summary failed")),
    )

    try:
        async with Client(server.mcp, name="post-success-failure-tester") as client:
            result = await client.call_tool(
                "tavily-search",
                {"query": "upstream succeeded"},
                raise_on_error=False,
            )

        assert result.is_error
        row = tracker.get_recent_requests()[0]
        assert row["status"] == "failed"
        assert row["credits"] == 1
        assert row["error_code"] == "RuntimeError"
    finally:
        tracker.close()


def test_main_omits_network_arguments_for_stdio(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server.settings, "transport", "stdio")
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main()

    assert calls == [{"transport": "stdio"}]


def test_main_passes_network_arguments_for_http(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server.settings, "transport", "http")
    monkeypatch.setattr(server.settings, "host", "127.0.0.1")
    monkeypatch.setattr(server.settings, "port", 8765)
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main()

    assert calls == [{"transport": "http", "host": "127.0.0.1", "port": 8765}]
