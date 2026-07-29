"""deep-research MCP server — drop-in Tavily replacement with multi-key routing."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import sys
import time
from typing import Annotated, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastmcp import Context, FastMCP
from pydantic import Field

from deep_research.audit import requester_metadata
from deep_research.auth import ConfiguredTokenVerifier
from deep_research.burst import SessionBurstLimiter
from deep_research.config import Settings
from deep_research.credits import CreditTracker, estimate_credits
from deep_research.fallback import SearchFallback
from deep_research.router import KeyRouter
from deep_research.tavily_client import TavilyAPIError, TavilyClient

logger = logging.getLogger("deep-research")

# ── bootstrap ──────────────────────────────────────────────────

settings = Settings()

tracker = CreditTracker(
    db_path=settings.db_path,
    request_log_retention_days=settings.audit_retention_days,
)
audit_tracker = (
    tracker
    if settings.db_path == ":memory:"
    else CreditTracker(
        db_path=settings.db_path,
        busy_timeout_seconds=settings.audit_busy_timeout_seconds,
        request_log_retention_days=settings.audit_retention_days,
    )
)
router = KeyRouter(
    keys=settings.api_keys,
    credits_per_key=settings.credits_per_key,
    tracker=tracker,
    cooldown_hours=settings.cooldown_hours,
)
client = TavilyClient(base_url=settings.tavily_base_url)
burst_limiter = SessionBurstLimiter(
    limit=settings.session_burst_limit,
    window_seconds=settings.session_burst_window_seconds,
)

auth = ConfiguredTokenVerifier(settings.auth_token) if settings.auth_token else None
fallback = SearchFallback(
    dataforseo_auth=settings.dataforseo_auth,
    dataforseo_base_url=settings.dataforseo_base_url,
    location_name=settings.dataforseo_location_name,
    llm_api_key=settings.fallback_llm_api_key,
    llm_base_url=settings.fallback_llm_base_url,
    llm_model=settings.fallback_llm_model,
    llm_input_cost_per_million=settings.fallback_llm_input_cost_per_million,
    llm_output_cost_per_million=settings.fallback_llm_output_cost_per_million,
    jina_base_url=settings.jina_scraper_base_url,
    content_max_chars=settings.fallback_content_max_chars,
    search_content_max_chars=settings.fallback_search_content_max_chars,
    extract_total_max_chars=settings.fallback_extract_total_max_chars,
    tracker=tracker,
    daily_cost_limit_usd=settings.fallback_daily_cost_limit_usd,
    max_concurrency=settings.fallback_max_concurrency,
    max_cost_per_search_usd=settings.fallback_max_cost_per_search_usd,
    jina_max_response_bytes=settings.jina_max_response_bytes,
) if settings.fallback_enabled else None

mcp = FastMCP(name="deep-research", auth=auth)


# ── internal routing ───────────────────────────────────────────

_MAX_RETRIES = 3


async def _route_request(endpoint: str, params: dict, ctx: Context) -> dict:
    """Select a key, forward the request and persist an attributed audit row."""
    estimated = estimate_credits(endpoint, params)
    started = time.monotonic()
    attempts = 0
    credits_used = None
    last_rate_limit_error = None
    audit_id = None
    upstream_succeeded = False
    fallback_audit: dict[str, object] | None = None
    audit_start = asyncio.create_task(_start_audit(endpoint, params, ctx))

    try:
        audit_id = await asyncio.shield(audit_start)
        burst_limiter.check(_context_session_id(ctx))
        for _attempt in range(max(_MAX_RETRIES, router.key_count)):
            try:
                key = await router.get_key()
            except RuntimeError:
                break
            attempts += 1
            try:
                result = await client.request(endpoint, key, params)
                actual = result.get("usage", {}).get("credits", estimated)
                credits_used = actual
                upstream_succeeded = True
                await router.report_usage(key, actual)
                result["_credits_remaining"] = await _credits_summary()
                await _finish_audit(
                    audit_id,
                    status="succeeded",
                    credits=actual,
                    attempts=attempts,
                    started=started,
                )
                return result
            except TavilyAPIError as exc:
                if exc.status_code == 429:
                    logger.warning(
                        "Key %s...%s hit 429 — cooling down for %sh",
                        key[:8],
                        key[-4:],
                        settings.cooldown_hours,
                    )
                    await router.mark_rate_limited(key)
                    last_rate_limit_error = exc
                    continue
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    logger.warning(
                        "Key %s...%s hit 429 — cooling down for %sh",
                        key[:8],
                        key[-4:],
                        settings.cooldown_hours,
                    )
                    await router.mark_rate_limited(key)
                    last_rate_limit_error = exc
                    continue
                raise

        if endpoint in {"search", "extract"} and settings.fallback_enabled:
            fallback_audit = {
                "provider": (
                    "dataforseo+llm+jina" if endpoint == "search" else "llm+jina"
                )
            }
            result = await _run_fallback(endpoint, params)
            fallback_audit.update(_fallback_audit_metadata(endpoint, result))
            credits_used = 0
            upstream_succeeded = True
            result["_credits_remaining"] = await _credits_summary()
            await _finish_audit(
                audit_id,
                status="succeeded",
                credits=0,
                attempts=attempts,
                started=started,
                fallback=fallback_audit,
            )
            return result
        if last_rate_limit_error is not None:
            raise last_rate_limit_error
        raise RuntimeError("All API keys are exhausted or in cooldown")
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(
            _finalize_cancelled_audit(
                audit_start,
                audit_id=audit_id,
                credits=credits_used,
                attempts=attempts,
                started=started,
                upstream_succeeded=upstream_succeeded,
                fallback=fallback_audit,
            )
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # FastMCP may deliver cancellation more than once. The independent
            # cleanup task remains responsible for obtaining the row ID and
            # performing the bounded, off-thread terminal update.
            pass
        raise
    except Exception as exc:
        await _finish_audit(
            audit_id,
            status="failed",
            credits=credits_used,
            attempts=attempts,
            started=started,
            error_code=_error_code(exc),
            fallback=fallback_audit,
        )
        raise


async def _run_fallback(endpoint: str, params: dict) -> dict:
    """Run a configured fallback after Tavily becomes unavailable."""
    if fallback is None:
        raise RuntimeError("Search fallback is not initialized")
    reason = "All Tavily API keys are exhausted or in cooldown"
    logger.warning("%s; activating %s fallback", reason, endpoint)
    if endpoint == "search":
        return await fallback.search(params["query"], params, reason)
    if endpoint == "extract":
        return await fallback.extract(params["urls"], params, reason)
    raise RuntimeError(f"No fallback is available for {endpoint}")


# ── tools (Tavily MCP interface, minus research) ───────────────


@mcp.tool(name="tavily-search")
async def tavily_search(
    query: Annotated[str, Field(description="Search query")],
    ctx: Context,
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
                "Include the cleaned and parsed HTML content of each search result"
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
    """Search the web for current information on any topic. This is a metered tool: do not invoke Tavily tools in parallel, prefer one broad query, and use at most three Tavily calls per research task unless the user approves more within the server's hard burst limit. Returns snippets and source URLs."""
    params = _strip_none(locals())
    return await _route_request("search", params, ctx)


@mcp.tool(name="tavily-extract")
async def tavily_extract(
    urls: Annotated[
        list[str],
        Field(description="List of URLs to extract content from"),
    ],
    ctx: Context,
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
    """Extract content from URLs. This is a metered tool: batch useful URLs into one call, never invoke Tavily tools in parallel, and stay within three Tavily calls per research task unless the user approves more within the server's hard burst limit. Returns raw page content in markdown or text format."""
    params = _strip_none(locals())
    return await _route_request("extract", params, ctx)


@mcp.tool(name="tavily-crawl")
async def tavily_crawl(
    url: Annotated[
        str,
        Field(description="The root URL to begin the crawl"),
    ],
    ctx: Context,
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
                "Max number of links to follow per level of the tree (i.e., per page)"
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
    """Crawl a website starting from a URL. This is a metered tool: do not invoke Tavily tools in parallel and stay within three Tavily calls per research task unless the user approves more within the server's hard burst limit. Extracts content from pages with configurable depth and breadth."""
    params = _strip_none(locals())
    return await _route_request("crawl", params, ctx)


@mcp.tool(name="tavily-map")
async def tavily_map(
    url: Annotated[
        str,
        Field(description="The root URL to begin the mapping"),
    ],
    ctx: Context,
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
                "Max number of links to follow per level of the tree (i.e., per page)"
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
    """Map a website's structure. This is a metered tool: do not invoke Tavily tools in parallel and stay within three Tavily calls per research task unless the user approves more within the server's hard burst limit. Returns a list of URLs found from the base URL."""
    params = _strip_none(locals())
    return await _route_request("map", params, ctx)


# ── bonus: credit-status tool ──────────────────────────────────


@mcp.tool(name="credit-status")
async def credit_status() -> dict:
    """Show remaining Tavily API credits across all configured keys, including per-key and total utilization percentage."""
    keys = await router.get_status_async()
    total_remaining = sum(k["remaining"] for k in keys)
    total_limit = sum(k["limit"] for k in keys)
    total_used = sum(k["used"] for k in keys)
    total_utilization = (
        round(total_used / total_limit * 100, 1) if total_limit > 0 else 0
    )
    return {
        "keys": keys,
        "total_used": total_used,
        "total_remaining": total_remaining,
        "total_limit": total_limit,
        "total_utilization_pct": total_utilization,
        "fallback_enabled": settings.fallback_enabled,
        "fallback_spend_today_usd": tracker.get_fallback_spend(),
        "fallback_daily_limit_usd": settings.fallback_daily_cost_limit_usd,
        "fallback_requests": tracker.get_fallback_request_summary(),
    }


# ── helpers ────────────────────────────────────────────────────


def _context_session_id(ctx: Context) -> str | None:
    """Read a bounded MCP session identifier without affecting availability."""
    try:
        value = ctx.session_id
    except RuntimeError:
        return None
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned[:1024] or None


async def _start_audit(endpoint: str, params: dict, ctx: Context) -> int | None:
    """Start audit logging without making observability an availability risk."""
    try:
        query = params.get("query")
        if query is None:
            query = params.get("instructions")
        if query is not None and not isinstance(query, str):
            query = str(query)
        query = _bounded_audit_text(query)
        target = _audit_target(params)
        metadata = requester_metadata(
            ctx, trust_proxy_headers=settings.trust_proxy_headers
        )
        return await asyncio.to_thread(
            audit_tracker.start_request,
            endpoint=endpoint,
            query=query,
            target=target,
            requester=metadata.as_dict(),
        )
    except Exception:
        logger.exception("Failed to start request audit row")
        return None


async def _finalize_cancelled_audit(
    audit_start: asyncio.Task[int | None],
    *,
    audit_id: int | None,
    attempts: int,
    started: float,
    credits: int | None,
    upstream_succeeded: bool,
    fallback: dict | None,
) -> None:
    """Finalize cancellation independently of repeated request-task cancellation."""
    if audit_id is None:
        audit_id = await asyncio.shield(audit_start)
    await _finish_audit(
        audit_id,
        status="succeeded" if upstream_succeeded else "cancelled",
        credits=credits,
        attempts=attempts,
        started=started,
        error_code=None if upstream_succeeded else "CancelledError",
        fallback=fallback,
    )


async def _finish_audit(
    audit_id: int | None,
    *,
    status: str,
    attempts: int,
    started: float,
    credits: int | None = None,
    error_code: str | None = None,
    fallback: dict | None = None,
) -> None:
    await asyncio.to_thread(
        _finish_audit_sync,
        audit_id,
        status=status,
        attempts=attempts,
        started=started,
        credits=credits,
        error_code=error_code,
        fallback=fallback,
    )


def _finish_audit_sync(
    audit_id: int | None,
    *,
    status: str,
    attempts: int,
    started: float,
    credits: int | None = None,
    error_code: str | None = None,
    fallback: dict | None = None,
) -> None:
    if audit_id is None:
        return
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    try:
        audit_tracker.finish_request(
            audit_id,
            status=status,
            credits=credits,
            attempts=attempts,
            duration_ms=duration_ms,
            error_code=error_code,
            fallback=fallback,
        )
    except Exception:
        logger.exception("Failed to finish request audit row %s", audit_id)


def _error_code(exc: Exception) -> str:
    """Return a bounded, non-sensitive failure code for the audit row."""
    if isinstance(exc, TavilyAPIError):
        return f"tavily_http_{exc.status_code}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return type(exc).__name__[:128]


def _fallback_audit_metadata(endpoint: str, result: dict) -> dict[str, object]:
    """Extract bounded fallback outcome fields from a successful response."""
    metadata = result.get("_fallback") or {}
    costs = metadata.get("cost_usd") or {}
    if endpoint == "search":
        returned = metadata.get("sources_returned", 0)
        failed = metadata.get("scrape_failures", 0)
    else:
        returned = metadata.get("urls_returned", 0)
        failed = metadata.get("urls_failed", 0)
    return {
        "cost_usd": costs.get("total", 0.0),
        "items_returned": int(returned),
        "items_failed": int(failed),
    }


def _audit_target(params: dict) -> str | None:
    """Return bounded URL targets without secret-bearing components."""
    if "url" in params:
        return _bounded_audit_text(_sanitize_url(str(params["url"])))
    if "urls" in params:
        urls = params["urls"]
        if not isinstance(urls, list):
            urls = [urls]
        sanitized = [_sanitize_url(str(url)) for url in urls]
        return _bounded_audit_text(json.dumps(sanitized, separators=(",", ":")))
    return None


def _sanitize_url(value: str) -> str:
    """Store a validated URL origin and non-reversible path fingerprint."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return "[invalid URL]"
        if any(
            character in parsed.netloc for character in ("%", "\\", "\r", "\n", "\t")
        ):
            return "[invalid URL]"
        hostname = parsed.hostname
        if not hostname or not _valid_url_host(hostname):
            return "[invalid URL]"
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parsed.port
        except ValueError:
            return "[invalid URL]"
        netloc = f"{host}:{port}" if port is not None else host
        path_fingerprint = ""
        if parsed.path and parsed.path != "/":
            digest = hashlib.sha256(parsed.path.encode()).hexdigest()[:16]
            path_fingerprint = f"/[path-sha256:{digest}]"
        return urlunsplit((parsed.scheme.lower(), netloc, path_fingerprint, "", ""))
    except ValueError:
        return "[invalid URL]"


def _valid_url_host(host: str) -> bool:
    """Accept IP literals or conservative ASCII DNS hostnames."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    candidate = host.removesuffix(".")
    if not candidate or len(candidate) > 253:
        return False
    labels = candidate.split(".")
    return all(
        label.isascii()
        and 1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )


def _bounded_audit_text(value: str | None) -> str | None:
    """Bound the persisted copy while retaining a fingerprint of full input."""
    if value is None or len(value) <= settings.audit_max_text_chars:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()
    suffix = f"...[truncated sha256:{digest}]"
    prefix_length = max(0, settings.audit_max_text_chars - len(suffix))
    return value[:prefix_length] + suffix


def _strip_none(d: dict) -> dict:
    """Remove None values from a dict (used to build API payloads)."""
    return {k: v for k, v in d.items() if v is not None and k != "ctx"}


async def _credits_summary() -> str:
    """One-line credit summary appended to every tool response."""
    statuses = await router.get_status_async()
    total_used = sum(s["used"] for s in statuses)
    total_limit = sum(s["limit"] for s in statuses)
    total_remaining = total_limit - total_used
    pct = round(total_remaining / total_limit * 100, 1) if total_limit > 0 else 0
    return f"{total_remaining}/{total_limit} credits remaining ({pct}%)"


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
    logger.info("Search fallback is %s", "ENABLED" if settings.fallback_enabled else "disabled")
    if settings.auth_token:
        logger.info("Bearer token auth is ENABLED")
    elif settings.allow_unauthenticated_http:
        logger.warning("Unauthenticated network transport explicitly enabled")

    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
