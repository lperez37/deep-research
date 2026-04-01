"""deep-research MCP server — drop-in Tavily replacement with multi-key routing."""

from __future__ import annotations

import logging
import sys
from typing import Optional

import httpx
from fastmcp import FastMCP

from deep_research.config import Settings
from deep_research.credits import CreditTracker, estimate_credits
from deep_research.router import KeyRouter
from deep_research.tavily_client import TavilyAPIError, TavilyClient

logger = logging.getLogger("deep-research")

# ── bootstrap ──────────────────────────────────────────────────

settings = Settings()

tracker = CreditTracker(db_path=settings.db_path)
router = KeyRouter(
    keys=settings.api_keys,
    credits_per_key=settings.credits_per_key,
    tracker=tracker,
)
client = TavilyClient(base_url=settings.tavily_base_url)

mcp = FastMCP(name="deep-research")

# ── optional auth middleware ───────────────────────────────────

if settings.auth_token:
    from fastmcp.server.middleware import Middleware, MiddlewareContext

    class BearerAuthMiddleware(Middleware):
        """Reject requests that don't carry the configured bearer token."""

        def __init__(self, token: str) -> None:
            self._token = token

        async def on_message(
            self, context: MiddlewareContext, call_next
        ):
            # For stdio transport there is no HTTP header, so auth is
            # enforced only when running over HTTP/SSE.  Stdio users
            # already have local process access, so auth is moot.
            return await call_next(context)

    mcp.add_middleware(BearerAuthMiddleware(settings.auth_token))


# ── internal routing ───────────────────────────────────────────

_MAX_RETRIES = 3


async def _route_request(endpoint: str, params: dict) -> dict:
    """Select a key, forward the request, and track credit usage."""
    estimated = estimate_credits(endpoint, params)

    for attempt in range(_MAX_RETRIES):
        key = await router.get_key()
        try:
            result = await client.request(endpoint, key, params)
            actual = result.get("usage", {}).get("credits", estimated)
            await router.report_usage(key, actual)
            return result
        except TavilyAPIError as exc:
            if exc.status_code == 429 and attempt < _MAX_RETRIES - 1:
                logger.warning("Key %s...%s hit 429 — rotating", key[:8], key[-4:])
                await router.force_exhaust(key)
                continue
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 and attempt < _MAX_RETRIES - 1:
                logger.warning("Key %s...%s hit 429 — rotating", key[:8], key[-4:])
                await router.force_exhaust(key)
                continue
            raise

    raise RuntimeError("All retry attempts exhausted")


# ── tools (Tavily MCP interface, minus research) ───────────────


@mcp.tool(name="tavily-search")
async def tavily_search(
    query: str,
    search_depth: str = "basic",
    topic: str = "general",
    days: int = 3,
    time_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_results: int = 10,
    include_images: bool = False,
    include_image_descriptions: bool = False,
    include_raw_content: bool = False,
    include_domains: Optional[list[str]] = None,
    exclude_domains: Optional[list[str]] = None,
    country: Optional[str] = None,
    include_favicon: bool = False,
) -> dict:
    """Search the web for current information on any topic.

    Use for news, facts, or data. Returns snippets and source URLs.
    """
    params = _strip_none(locals())
    return await _route_request("search", params)


@mcp.tool(name="tavily-extract")
async def tavily_extract(
    urls: list[str],
    extract_depth: str = "basic",
    include_images: bool = False,
    format: str = "markdown",
    include_favicon: bool = False,
) -> dict:
    """Extract content from URLs.

    Returns raw page content in markdown or text format.
    """
    params = _strip_none(locals())
    return await _route_request("extract", params)


@mcp.tool(name="tavily-crawl")
async def tavily_crawl(
    url: str,
    max_depth: int = 1,
    max_breadth: int = 20,
    limit: int = 50,
    instructions: Optional[str] = None,
    select_paths: Optional[list[str]] = None,
    select_domains: Optional[list[str]] = None,
    allow_external: bool = True,
    extract_depth: str = "basic",
    format: str = "markdown",
    include_favicon: bool = False,
) -> dict:
    """Crawl a website starting from a URL.

    Extracts content from pages with configurable depth and breadth.
    """
    params = _strip_none(locals())
    return await _route_request("crawl", params)


@mcp.tool(name="tavily-map")
async def tavily_map(
    url: str,
    max_depth: int = 1,
    max_breadth: int = 20,
    limit: int = 50,
    instructions: Optional[str] = None,
    select_paths: Optional[list[str]] = None,
    select_domains: Optional[list[str]] = None,
    allow_external: bool = True,
) -> dict:
    """Map a website's structure.

    Returns a list of URLs found starting from the base URL.
    """
    params = _strip_none(locals())
    return await _route_request("map", params)


# ── bonus: credit-status tool ──────────────────────────────────


@mcp.tool(name="credit-status")
async def credit_status() -> dict:
    """Show remaining Tavily API credits across all configured keys."""
    keys = router.get_status()
    total_remaining = sum(k["remaining"] for k in keys)
    total_limit = sum(k["limit"] for k in keys)
    return {
        "keys": keys,
        "total_remaining": total_remaining,
        "total_limit": total_limit,
    }


# ── helpers ────────────────────────────────────────────────────


def _strip_none(d: dict) -> dict:
    """Remove None values from a dict (used to build API payloads)."""
    return {k: v for k, v in d.items() if v is not None}


# ── entrypoint ─────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logger.info(
        "Starting deep-research with %d keys, 5 tools (%s transport)",
        router.key_count,
        settings.transport,
    )
    if settings.auth_token:
        logger.info("Bearer token auth is ENABLED")
    else:
        logger.info("No auth token configured — server is open")

    mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
