"""deep-research MCP server — drop-in Tavily replacement with multi-key routing."""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Literal, Optional

import httpx
from fastmcp import FastMCP
from pydantic import Field

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
    query: Annotated[str, Field(description="Search query")],
    search_depth: Annotated[
        Literal["basic", "advanced", "fast", "ultra-fast"],
        Field(
            default="basic",
            description=(
                "The depth of the search. 'basic' for generic results, "
                "'advanced' for more thorough search, 'fast' for optimized "
                "low latency with high relevance, 'ultra-fast' for "
                "prioritizing latency above all else"
            ),
        ),
    ] = "basic",
    topic: Annotated[
        Literal["general", "news", "finance"],
        Field(
            default="general",
            description=(
                "The category of the search. 'news' is useful for retrieving "
                "real-time updates, particularly about politics, sports, and "
                "major current events. 'finance' is for financial information. "
                "'general' is for broader, more general-purpose searches."
            ),
        ),
    ] = "general",
    days: Annotated[
        int,
        Field(default=3, description="Number of days back to search"),
    ] = 3,
    time_range: Annotated[
        Optional[Literal["day", "week", "month", "year"]],
        Field(
            default=None,
            description=(
                "The time range back from the current date to include "
                "in the search results"
            ),
        ),
    ] = None,
    start_date: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Will return all results after the specified start date. "
                "Required to be written in the format YYYY-MM-DD."
            ),
        ),
    ] = None,
    end_date: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Will return all results before the specified end date. "
                "Required to be written in the format YYYY-MM-DD"
            ),
        ),
    ] = None,
    max_results: Annotated[
        int,
        Field(
            default=5,
            ge=5,
            le=20,
            description="The maximum number of search results to return",
        ),
    ] = 5,
    include_images: Annotated[
        bool,
        Field(
            default=False,
            description="Include a list of query-related images in the response",
        ),
    ] = False,
    include_image_descriptions: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Include a list of query-related images and their "
                "descriptions in the response"
            ),
        ),
    ] = False,
    include_raw_content: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Include the cleaned and parsed HTML content of each "
                "search result"
            ),
        ),
    ] = False,
    include_domains: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "A list of domains to specifically include in the search "
                "results, if the user asks to search on specific sites set "
                "this to the domain of the site"
            ),
        ),
    ] = None,
    exclude_domains: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "List of domains to specifically exclude, if the user asks "
                "to exclude a domain set this to the domain of the site"
            ),
        ),
    ] = None,
    country: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Boost search results from a specific country. Must be a "
                "full country name (e.g., 'United States', 'Japan', "
                "'Germany'). ISO country codes (e.g., 'us', 'jp') are not "
                "supported. Available only if topic is general. See "
                "https://docs.tavily.com/documentation/api-reference/search "
                "for the full list of supported countries."
            ),
        ),
    ] = None,
    include_favicon: Annotated[
        bool,
        Field(
            default=False,
            description="Whether to include the favicon URL for each result",
        ),
    ] = False,
) -> dict:
    """Search the web for current information on any topic. Use for news, facts, or data beyond your knowledge cutoff. Returns snippets and source URLs."""
    params = _strip_none(locals())
    return await _route_request("search", params)


@mcp.tool(name="tavily-extract")
async def tavily_extract(
    urls: Annotated[
        list[str],
        Field(description="List of URLs to extract content from"),
    ],
    query: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Query to rerank content chunks by relevance. When provided, "
                "chunks are reranked based on relevance to this query."
            ),
        ),
    ] = None,
    extract_depth: Annotated[
        Literal["basic", "advanced"],
        Field(
            default="basic",
            description=(
                "Use 'advanced' for LinkedIn, protected sites, or "
                "tables/embedded content"
            ),
        ),
    ] = "basic",
    include_images: Annotated[
        bool,
        Field(default=False, description="Include images from pages"),
    ] = False,
    format: Annotated[
        Literal["markdown", "text"],
        Field(default="markdown", description="Output format"),
    ] = "markdown",
    include_favicon: Annotated[
        bool,
        Field(default=False, description="Include favicon URLs"),
    ] = False,
) -> dict:
    """Extract content from URLs. Returns raw page content in markdown or text format."""
    params = _strip_none(locals())
    return await _route_request("extract", params)


@mcp.tool(name="tavily-crawl")
async def tavily_crawl(
    url: Annotated[
        str,
        Field(description="The root URL to begin the crawl"),
    ],
    max_depth: Annotated[
        int,
        Field(
            default=1,
            ge=1,
            description=(
                "Max depth of the crawl. Defines how far from the base "
                "URL the crawler can explore."
            ),
        ),
    ] = 1,
    max_breadth: Annotated[
        int,
        Field(
            default=20,
            ge=1,
            description=(
                "Max number of links to follow per level of the tree "
                "(i.e., per page)"
            ),
        ),
    ] = 20,
    limit: Annotated[
        int,
        Field(
            default=50,
            ge=1,
            description=(
                "Total number of links the crawler will process before stopping"
            ),
        ),
    ] = 50,
    instructions: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Natural language instructions for the crawler. Instructions "
                "specify which types of pages the crawler should return."
            ),
        ),
    ] = None,
    select_paths: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Regex patterns to select only URLs with specific path "
                "patterns (e.g., /docs/.*, /api/v1.*)"
            ),
        ),
    ] = None,
    select_domains: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Regex patterns to restrict crawling to specific domains "
                "or subdomains (e.g., ^docs\\.example\\.com$)"
            ),
        ),
    ] = None,
    allow_external: Annotated[
        bool,
        Field(
            default=True,
            description="Whether to return external links in the final response",
        ),
    ] = True,
    extract_depth: Annotated[
        Literal["basic", "advanced"],
        Field(
            default="basic",
            description=(
                "Advanced extraction retrieves more data, including tables "
                "and embedded content, with higher success but may increase "
                "latency"
            ),
        ),
    ] = "basic",
    format: Annotated[
        Literal["markdown", "text"],
        Field(
            default="markdown",
            description=(
                "The format of the extracted web page content. markdown "
                "returns content in markdown format. text returns plain "
                "text and may increase latency."
            ),
        ),
    ] = "markdown",
    include_favicon: Annotated[
        bool,
        Field(
            default=False,
            description="Whether to include the favicon URL for each result",
        ),
    ] = False,
) -> dict:
    """Crawl a website starting from a URL. Extracts content from pages with configurable depth and breadth."""
    params = _strip_none(locals())
    return await _route_request("crawl", params)


@mcp.tool(name="tavily-map")
async def tavily_map(
    url: Annotated[
        str,
        Field(description="The root URL to begin the mapping"),
    ],
    max_depth: Annotated[
        int,
        Field(
            default=1,
            ge=1,
            description=(
                "Max depth of the mapping. Defines how far from the base "
                "URL the crawler can explore"
            ),
        ),
    ] = 1,
    max_breadth: Annotated[
        int,
        Field(
            default=20,
            ge=1,
            description=(
                "Max number of links to follow per level of the tree "
                "(i.e., per page)"
            ),
        ),
    ] = 20,
    limit: Annotated[
        int,
        Field(
            default=50,
            ge=1,
            description=(
                "Total number of links the crawler will process before stopping"
            ),
        ),
    ] = 50,
    instructions: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Natural language instructions for the crawler",
        ),
    ] = None,
    select_paths: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Regex patterns to select only URLs with specific path "
                "patterns (e.g., /docs/.*, /api/v1.*)"
            ),
        ),
    ] = None,
    select_domains: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Regex patterns to restrict crawling to specific domains "
                "or subdomains (e.g., ^docs\\.example\\.com$)"
            ),
        ),
    ] = None,
    allow_external: Annotated[
        bool,
        Field(
            default=True,
            description="Whether to return external links in the final response",
        ),
    ] = True,
) -> dict:
    """Map a website's structure. Returns a list of URLs found starting from the base URL."""
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
