"""Small, bounded fallbacks for Tavily search and extraction."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from deep_research.credits import CreditTracker


class SearchFallback:
    """Return provider results directly without a second research pipeline."""

    _SERP_PATH = "/serp/google/organic/live/advanced"
    _URL_MAX_CHARS = 2_048

    def __init__(
        self,
        *,
        dataforseo_auth: str,
        dataforseo_base_url: str,
        location_name: str,
        jina_base_url: str,
        content_max_chars: int,
        extract_total_max_chars: int,
        tracker: CreditTracker,
        daily_cost_limit_usd: float,
        max_concurrency: int,
        max_cost_per_search_usd: float,
        jina_max_response_bytes: int,
    ) -> None:
        self._dataforseo_auth = dataforseo_auth
        self._location_name = location_name
        self._content_max_chars = content_max_chars
        self._extract_total_max_chars = extract_total_max_chars
        self._tracker = tracker
        self._daily_cost_limit_usd = daily_cost_limit_usd
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cost_reservation_usd = max_cost_per_search_usd
        self._jina_max_response_bytes = jina_max_response_bytes
        timeout = httpx.Timeout(12.0, connect=5.0)
        self._serp_http = httpx.AsyncClient(
            base_url=dataforseo_base_url.rstrip("/"), timeout=timeout
        )
        self._jina_http = httpx.AsyncClient(
            base_url=jina_base_url.rstrip("/"), timeout=timeout
        )

    async def search(self, query: str, params: dict, reason: str) -> dict:
        """Return a Tavily-shaped response from one DataForSEO request."""
        if len(query) > 500:
            raise ValueError("Fallback search query exceeds 500 characters")
        if any(
            len(params.get(name, [])) > 10
            for name in ("include_domains", "exclude_domains")
        ):
            raise ValueError("Fallback supports at most 10 domain filters")
        if params.get("topic", "general") != "general":
            raise ValueError("Fallback supports only general web searches")

        reservation_day = self._tracker.reserve_fallback_cost(
            self._cost_reservation_usd, self._daily_cost_limit_usd
        )
        if reservation_day is None:
            raise RuntimeError("Daily fallback spending limit reached")

        actual_cost = 0.0
        started = time.perf_counter()
        try:
            async with self._semaphore:
                results, actual_cost = await self._search_results(query, params)
        finally:
            # Failed and cancelled requests must not consume their full reservation.
            self._tracker.settle_fallback_cost(
                reservation_day, self._cost_reservation_usd, actual_cost
            )

        return {
            "query": query,
            "follow_up_questions": None,
            "answer": None,
            "images": [],
            "results": results,
            "response_time": round(time.perf_counter() - started, 3),
            "_fallback_notice": (
                "Tavily keys were unavailable; DataForSEO search fallback was "
                "activated. Results are Google organic snippets."
            ),
            "_fallback": {
                "activated": True,
                "reason": reason,
                "provider": "dataforseo",
                "sources_returned": len(results),
                "location": self._location_name,
                "requested_country": params.get("country"),
                "requested_country_ignored": bool(params.get("country")),
                "raw_content_requested_but_unavailable": bool(
                    params.get("include_raw_content")
                ),
                "daily_spend_usd": round(self._tracker.get_fallback_spend(), 6),
                "daily_limit_usd": self._daily_cost_limit_usd,
                "cost_usd": {
                    "dataforseo_serp": round(actual_cost, 8),
                    "total": round(actual_cost, 8),
                },
            },
        }

    async def _search_results(
        self, query: str, params: dict
    ) -> tuple[list[dict], float]:
        max_results = min(params.get("max_results", 5), 20)
        payload = [{
            "keyword": self._search_keyword(query, params),
            "location_name": self._location_name,
            "language_code": "en",
            "device": "desktop",
            "os": "windows",
            "depth": max(10, max_results * 2),
        }]
        response = await self._serp_http.post(
            self._SERP_PATH,
            json=payload,
            headers={"Authorization": f"Basic {self._dataforseo_auth}"},
        )
        response.raise_for_status()
        tasks = response.json().get("tasks") or []
        task = tasks[0] if tasks else {}
        cost = float(task.get("cost") or 0.0)
        if task.get("status_code") != 20000:
            message = task.get("status_message") or "missing task"
            if "no search results" in message.casefold():
                return [], cost
            raise RuntimeError(f"DataForSEO SERP request failed: {message}")

        provider_results = (task.get("result") or [{}])[0].get("items") or []
        includes = params.get("include_domains", [])
        excludes = params.get("exclude_domains", [])
        results: list[dict] = []
        seen_urls: set[str] = set()
        for item in provider_results:
            if item.get("type") != "organic" or not item.get("url"):
                continue
            url = self._safe_url(item["url"])
            if not url or url in seen_urls:
                continue
            hostname = (urlsplit(url).hostname or "").lower()
            if includes and not any(
                self._domain_matches(hostname, domain) for domain in includes
            ):
                continue
            if any(self._domain_matches(hostname, domain) for domain in excludes):
                continue
            seen_urls.add(url)
            rank = item.get("rank_group")
            if not isinstance(rank, int) or rank < 1:
                rank = len(results) + 1
            results.append({
                "title": str(item.get("title") or "Untitled")[:300],
                "url": url,
                "content": str(item.get("description") or "")[:1_000],
                "score": max(0.0, 1.0 - (rank - 1) * 0.05),
            })
            if len(results) == max_results:
                break
        return results, cost

    async def extract(self, urls: list[str], params: dict, reason: str) -> dict:
        """Return bounded Jina content directly, without LLM synthesis."""
        if len(urls) > 20:
            raise ValueError("Fallback extract supports at most 20 URLs")

        started = time.perf_counter()
        targets = [(url, self._safe_url(url)) for url in urls]
        valid = [(original, safe) for original, safe in targets if safe is not None]
        per_url_limit = min(
            self._content_max_chars,
            self._extract_total_max_chars // max(1, len(valid)),
        )
        async with self._semaphore:
            extracted = await asyncio.gather(
                *(
                    self._extract_url(
                        original, safe, params.get("format", "markdown"), per_url_limit
                    )
                    for original, safe in valid
                ),
                return_exceptions=True,
            )

        results = [item for item in extracted if isinstance(item, dict)]
        failed_results = [
            {"url": original, "error": "URL is not a public HTTP(S) target"}
            for original, safe in targets
            if safe is None
        ]
        failed_results.extend(
            {"url": original, "error": "Jina extraction failed"}
            for (original, _safe), item in zip(valid, extracted, strict=True)
            if isinstance(item, BaseException)
        )
        return {
            "results": results,
            "failed_results": failed_results,
            "response_time": round(time.perf_counter() - started, 3),
            "_fallback_notice": (
                "Tavily keys were unavailable; direct Jina extraction fallback "
                "was activated."
            ),
            "_fallback": {
                "activated": True,
                "reason": reason,
                "provider": "jina",
                "urls_requested": len(urls),
                "urls_returned": len(results),
                "urls_failed": len(failed_results),
                "content_limit_chars_per_url": per_url_limit,
                "cost_usd": {"jina_scraping": 0.0, "total": 0.0},
            },
        }

    async def _extract_url(
        self, original_url: str, safe_url: str, format: str, limit: int
    ) -> dict:
        body = await self._fetch_jina(safe_url)
        field = "content" if format == "text" else "markdown"
        content = body.get(field)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Jina returned no {format} content for {safe_url}")
        return {
            "url": original_url,
            "raw_content": content[:limit],
            "images": body.get("images") if isinstance(body.get("images"), list) else [],
        }

    async def _fetch_jina(self, url: str) -> dict:
        async with self._jina_http.stream("POST", "/process", json={"url": url}) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > self._jina_max_response_bytes:
                    raise RuntimeError("Jina response exceeded the byte limit")
        body = json.loads(chunks)
        if not isinstance(body, dict):
            raise RuntimeError("Jina returned an invalid response")
        return body

    @staticmethod
    def _search_keyword(query: str, params: dict) -> str:
        terms = [query]
        terms.extend(f"site:{domain}" for domain in params.get("include_domains", []))
        terms.extend(f"-site:{domain}" for domain in params.get("exclude_domains", []))
        if params.get("start_date"):
            terms.append(f"after:{params['start_date']}")
        if params.get("end_date"):
            terms.append(f"before:{params['end_date']}")
        if not params.get("start_date"):
            ranges = {"day": 1, "week": 7, "month": 30, "year": 365}
            time_range = params.get("time_range")
            days = ranges.get(time_range) if isinstance(time_range, str) else None
            if days:
                start = datetime.now(timezone.utc).date() - timedelta(days=days)
                terms.append(f"after:{start.isoformat()}")
        return " ".join(terms)

    @staticmethod
    def _domain_matches(hostname: str, domain: str) -> bool:
        normalized = domain.casefold().strip().lstrip(".")
        return hostname == normalized or hostname.endswith(f".{normalized}")

    @staticmethod
    def _safe_url(url: str) -> str | None:
        """Accept only public-looking HTTP URLs before passing them internally."""
        try:
            if len(url) > SearchFallback._URL_MAX_CHARS:
                return None
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return None
            if parsed.port not in {None, 80, 443}:
                return None
            hostname = parsed.hostname.lower().rstrip(".")
            if hostname == "localhost" or hostname.endswith(".localhost"):
                return None
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                pass
            else:
                if not address.is_global:
                    return None
            return urlunsplit((
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            ))
        except ValueError:
            return None

    async def close(self) -> None:
        """Close both upstream HTTP pools."""
        await asyncio.gather(self._serp_http.aclose(), self._jina_http.aclose())
