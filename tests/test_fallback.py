"""Tests for the direct DataForSEO and Jina fallbacks."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from deep_research.credits import CreditTracker
from deep_research.fallback import SearchFallback


@pytest.fixture
async def fallback():
    tracker = CreditTracker(":memory:")
    client = SearchFallback(
        dataforseo_auth="test-auth",
        dataforseo_base_url="https://dataforseo.test/v3",
        location_name="Amsterdam,North Holland,Netherlands",
        jina_base_url="http://jina.test",
        content_max_chars=20,
        extract_total_max_chars=30,
        tracker=tracker,
        daily_cost_limit_usd=1.0,
        max_concurrency=2,
        max_cost_per_search_usd=0.02,
        jina_max_response_bytes=1_100_000,
    )
    yield client
    await client.close()


def _serp_response(count: int = 10) -> dict:
    return {
        "tasks": [{
            "status_code": 20000,
            "status_message": "Ok.",
            "cost": 0.002,
            "result": [{
                "items": [
                    {
                        "type": "organic",
                        "rank_group": index + 1,
                        "title": f"Result {index}",
                        "url": f"https://source{index}.example/article",
                        "description": f"Snippet {index}",
                    }
                    for index in range(count)
                ]
            }],
        }]
    }


@respx.mock
async def test_search_returns_provider_results_without_scraping_or_llm(
    fallback: SearchFallback,
):
    route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=_serp_response()))

    result = await fallback.search(
        "useful query", {"max_results": 5, "include_raw_content": True},
        "keys exhausted",
    )

    assert len(result["results"]) == 5
    assert result["results"][0] == {
        "title": "Result 0",
        "url": "https://source0.example/article",
        "content": "Snippet 0",
        "score": 1.0,
    }
    assert result["answer"] is None
    assert result["_fallback"]["provider"] == "dataforseo"
    assert result["_fallback"]["raw_content_requested_but_unavailable"] is True
    assert result["_fallback"]["cost_usd"] == {
        "dataforseo_serp": 0.002,
        "total": 0.002,
    }
    payload = json.loads(route.calls.last.request.content)[0]
    assert payload["keyword"] == "useful query"
    assert payload["depth"] == 10


@respx.mock
async def test_search_applies_dates_and_domain_filters(fallback: SearchFallback):
    response = _serp_response(3)
    response["tasks"][0]["result"][0]["items"][1]["url"] = (
        "https://excluded.example/article"
    )
    route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=response))

    result = await fallback.search(
        "query",
        {
            "include_domains": ["source0.example"],
            "exclude_domains": ["excluded.example"],
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
        },
        "keys exhausted",
    )

    assert [item["url"] for item in result["results"]] == [
        "https://source0.example/article"
    ]
    keyword = json.loads(route.calls.last.request.content)[0]["keyword"]
    assert keyword == (
        "query site:source0.example -site:excluded.example "
        "after:2026-01-01 before:2026-02-01"
    )


@respx.mock
async def test_no_serp_results_is_an_empty_success(fallback: SearchFallback):
    respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json={"tasks": [{
        "status_code": 40101,
        "status_message": "No Search Results.",
        "cost": 0.002,
    }]}))

    result = await fallback.search("unknown query", {}, "keys exhausted")

    assert result["results"] == []
    assert result["_fallback"]["cost_usd"]["total"] == 0.002


@respx.mock
async def test_provider_error_does_not_leak_cost_reservation(
    fallback: SearchFallback,
):
    respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json={"tasks": [{
        "status_code": 50000,
        "status_message": "Internal SE Server Error.",
        "cost": 0,
    }]}))

    with pytest.raises(RuntimeError, match="Internal SE Server Error"):
        await fallback.search("query", {}, "keys exhausted")

    assert fallback._tracker.get_fallback_spend() == 0.0


@respx.mock
async def test_search_does_not_retry_provider_error(fallback: SearchFallback):
    route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json={"tasks": [{
        "status_code": 50000,
        "status_message": "Internal SE Server Error.",
    }]}))

    with pytest.raises(RuntimeError):
        await fallback.search("query", {}, "keys exhausted")

    assert route.call_count == 1


@respx.mock
async def test_extract_returns_jina_content_and_partial_failures(
    fallback: SearchFallback,
):
    async def jina_response(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        if "blocked" in url:
            return httpx.Response(502, text="blocked")
        return httpx.Response(200, json={
            "markdown": "# Extracted Markdown that is bounded",
            "content": "Extracted text",
            "images": ["https://example.com/image.png"],
        })

    route = respx.post("http://jina.test/process").mock(side_effect=jina_response)
    result = await fallback.extract(
        [
            "https://good.example/page",
            "https://blocked.example/page",
            "http://127.0.0.1/private",
        ],
        {"format": "markdown"},
        "keys exhausted",
    )

    assert route.call_count == 2
    assert result["results"] == [{
        "url": "https://good.example/page",
        "raw_content": "# Extracted Mar",
        "images": ["https://example.com/image.png"],
    }]
    assert len(result["failed_results"]) == 2
    assert result["_fallback"]["provider"] == "jina"
    assert result["_fallback"]["content_limit_chars_per_url"] == 15


@respx.mock
async def test_extract_text_format_uses_plain_content(fallback: SearchFallback):
    respx.post("http://jina.test/process").mock(
        return_value=httpx.Response(200, json={
            "markdown": "# Markdown",
            "content": "Plain reader content",
        })
    )

    result = await fallback.extract(
        ["https://example.com"], {"format": "text"}, "keys exhausted"
    )

    assert result["results"][0]["raw_content"] == "Plain reader content"


def test_safe_url_rejects_internal_targets():
    assert SearchFallback._safe_url("http://127.0.0.1/admin") is None
    assert SearchFallback._safe_url("http://localhost/admin") is None
    assert SearchFallback._safe_url("file:///etc/passwd") is None
    assert SearchFallback._safe_url("https://example.com:8443/admin") is None
    assert SearchFallback._safe_url("https://example.com/" + "x" * 2100) is None
    assert (
        SearchFallback._safe_url("https://example.com/article#section")
        == "https://example.com/article"
    )
