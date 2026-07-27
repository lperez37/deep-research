"""Tests for configured HTTP bearer authentication."""

from __future__ import annotations

import httpx
from fastmcp import FastMCP

from deep_research.auth import ConfiguredTokenVerifier


async def test_verifier_accepts_only_exact_token() -> None:
    verifier = ConfiguredTokenVerifier("correct-horse-battery-staple")

    accepted = await verifier.verify_token("correct-horse-battery-staple")
    rejected = await verifier.verify_token("correct-horse-battery-stapled")

    assert accepted is not None
    assert accepted.client_id == "configured-bearer-token"
    assert rejected is None
    assert "correct-horse-battery-staple" not in repr(verifier.__dict__)


async def test_http_transport_rejects_missing_and_invalid_bearer_tokens() -> None:
    mcp = FastMCP(
        "authenticated-test",
        auth=ConfiguredTokenVerifier("expected-token"),
    )
    app = mcp.http_app(path="/mcp")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        missing = await client.post("/mcp", json={})
        invalid = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong-token"},
            json={},
        )
        authenticated = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer expected-token"},
            json={},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    # The empty JSON body is not a valid MCP message, but it reached MCP only
    # after authentication succeeded.
    assert authenticated.status_code != 401
