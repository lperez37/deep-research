"""Tests for the DataForSEO/LLM/Jina search fallback."""

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
        llm_api_key="test-llm-key",
        llm_base_url="https://llm.test/v1",
        llm_model="deepseek-v4-flash",
        llm_input_cost_per_million=0.14,
        llm_output_cost_per_million=0.28,
        jina_base_url="http://jina.test",
        content_max_chars=20,
        search_content_max_chars=10,
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
async def test_search_selects_and_scrapes_three_sources(fallback: SearchFallback):
    serp_route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=_serp_response()))
    llm_route = respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "selected": [
                    {"index": 0, "score": 0.98},
                    {"index": 2, "score": 0.91},
                    {"index": 5, "score": 0.84},
                ]
            })}}],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "total_tokens": 550,
            },
        })
    )

    async def jina_response(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        return httpx.Response(200, json={
            "title": f"Scraped {url}",
            "source_url": url,
            "markdown": "# Useful source\n\nLong content",
        })

    jina_route = respx.post("http://jina.test/process").mock(
        side_effect=jina_response
    )

    result = await fallback.search("useful query", {}, "keys exhausted")

    assert [item["score"] for item in result["results"]] == [0.98, 0.91, 0.84]
    assert all(item["content"] == "# Useful s" for item in result["results"])
    assert all("raw_content" not in item for item in result["results"])
    assert jina_route.call_count == 3
    assert result["_fallback"]["serp_candidates"] == 10
    assert result["_fallback"]["sources_returned"] == 3
    assert result["_fallback"]["content_limit_chars"] == 10
    assert result["_fallback"]["raw_content_limit_chars"] == 20
    assert result["_fallback"]["cost_usd"] == {
        "dataforseo_serp": 0.002,
        "llm_relevance_selection": 0.000084,
        "jina_scraping": 0.0,
        "total": 0.002084,
    }

    serp_payload = json.loads(serp_route.calls.last.request.content)[0]
    assert serp_payload["depth"] == 20
    assert serp_payload["location_name"] == "Amsterdam,North Holland,Netherlands"
    llm_payload = json.loads(llm_route.calls.last.request.content)
    assert len(json.loads(llm_payload["messages"][1]["content"])["candidates"]) == 10
    assert llm_payload["thinking"] == {"type": "disabled"}
    assert llm_payload["max_tokens"] == 500
    assert result["_fallback"]["selection_method"] == "llm"


@respx.mock
async def test_search_ignores_country_and_reports_amsterdam_location(
    fallback: SearchFallback,
):
    serp_route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=_serp_response()))
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": '{"selected":[]}'}}],
            "usage": {},
        })
    )

    result = await fallback.search(
        "Vencloud Spain software company",
        {"country": "Spain"},
        "keys exhausted",
    )

    payload = json.loads(serp_route.calls.last.request.content)[0]
    assert payload["location_name"] == "Amsterdam,North Holland,Netherlands"
    assert result["_fallback"]["requested_country"] == "Spain"
    assert result["_fallback"]["requested_country_ignored"] is True


@respx.mock
async def test_no_relevant_sources_skips_jina(fallback: SearchFallback):
    respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=_serp_response()))
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": '{"selected":[]}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5},
        })
    )
    jina_route = respx.post("http://jina.test/process")

    result = await fallback.search("unanswerable query", {}, "keys exhausted")

    assert result["results"] == []
    assert result["_fallback"]["sources_selected"] == 0
    assert not jina_route.called


@respx.mock
async def test_failed_scrape_does_not_discard_other_results(
    fallback: SearchFallback,
):
    respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=_serp_response()))
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "selected": [
                    {"index": 0, "score": 0.9},
                    {"index": 1, "score": 0.8},
                ]
            })}}],
            "usage": {},
        })
    )

    call_count = 0

    async def jina_response(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(502, text="blocked")
        return httpx.Response(200, json={"markdown": "valid", "title": "Valid"})

    respx.post("http://jina.test/process").mock(side_effect=jina_response)

    result = await fallback.search("query", {}, "keys exhausted")

    assert len(result["results"]) == 1
    assert result["_fallback"]["scrape_failures"] == 1


@respx.mock
async def test_all_scrapes_failed_raises(fallback: SearchFallback):
    respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=_serp_response()))
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": (
                '{"selected":[{"index":0,"score":0.9}]}'
            )}}],
            "usage": {},
        })
    )
    respx.post("http://jina.test/process").mock(
        return_value=httpx.Response(503, text="unavailable")
    )

    with pytest.raises(RuntimeError, match="every selected source"):
        await fallback.search("query", {}, "keys exhausted")


def test_safe_url_rejects_internal_targets():
    assert SearchFallback._safe_url("http://127.0.0.1/admin") is None
    assert SearchFallback._safe_url("http://localhost/admin") is None
    assert SearchFallback._safe_url("file:///etc/passwd") is None
    assert (
        SearchFallback._safe_url("https://example.com/article#section")
        == "https://example.com/article"
    )


@respx.mock
async def test_serp_internal_error_retries_once(fallback: SearchFallback):
    route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(side_effect=[
        httpx.Response(200, json={"tasks": [{
            "status_code": 50000,
            "status_message": "Internal SE Server Error.",
            "cost": 0,
        }]}),
        httpx.Response(200, json=_serp_response()),
    ])
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": '{"selected":[]}'}}],
            "usage": {},
        })
    )

    result = await fallback.search("query", {}, "keys exhausted")

    assert route.call_count == 2
    assert result["_fallback"]["cost_usd"]["dataforseo_serp"] == 0.002


@respx.mock
async def test_serp_no_results_retries_without_domain_token(
    fallback: SearchFallback,
):
    route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(side_effect=[
        httpx.Response(200, json={"tasks": [{
            "status_code": 40101,
            "status_message": "No Search Results.",
            "cost": 0.002,
        }]}),
        httpx.Response(200, json=_serp_response()),
    ])
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": '{"selected":[]}'}}],
            "usage": {},
        })
    )

    result = await fallback.search(
        "Properize properize.com company product", {}, "keys exhausted"
    )

    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)[0]["keyword"]
    second = json.loads(route.calls[1].request.content)[0]["keyword"]
    assert first == "Properize properize.com company product"
    assert second == "Properize company product"
    assert result["_fallback"]["cost_usd"]["dataforseo_serp"] == 0.004


@respx.mock
async def test_repeated_serp_no_results_returns_empty_success(
    fallback: SearchFallback,
):
    route = respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json={"tasks": [{
        "status_code": 40101,
        "status_message": "No Search Results.",
        "cost": 0.002,
    }]}))

    result = await fallback.search("unknown.example", {}, "keys exhausted")

    assert route.call_count == 2
    assert result["results"] == []
    assert result["_fallback"]["serp_candidates"] == 0


@respx.mock
async def test_null_llm_content_uses_google_rank_without_retry(
    fallback: SearchFallback,
):
    respx.post(
        "https://dataforseo.test/v3/serp/google/organic/live/advanced"
    ).mock(return_value=httpx.Response(200, json=_serp_response()))
    llm_route = respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": None}}],
            "usage": {"total_tokens": 500, "cost": 0.0001},
        })
    )
    respx.post("http://jina.test/process").mock(
        return_value=httpx.Response(200, json={"markdown": "valid"})
    )

    result = await fallback.search("query", {}, "keys exhausted")

    assert llm_route.call_count == 1
    assert len(result["results"]) == 3
    assert result["_fallback"]["llm"]["total_tokens"] == 500
    assert result["_fallback"]["cost_usd"]["llm_relevance_selection"] == 0.0001
    assert result["_fallback"]["selection_method"] == (
        "google_rank_after_invalid_llm"
    )


@respx.mock
async def test_extract_uses_jina_and_returns_partial_failures(
    fallback: SearchFallback,
):
    async def jina_response(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        if "blocked" in url:
            return httpx.Response(502, text="blocked")
        return httpx.Response(200, json={
            "markdown": "# Extracted Markdown",
            "content": "Extracted text",
        })

    route = respx.post("http://jina.test/process").mock(side_effect=jina_response)
    urls = [
        "https://good.example/page",
        "https://blocked.example/page",
        "http://127.0.0.1/private",
    ]

    result = await fallback.extract(
        urls,
        {"format": "markdown", "query": "ignored reranking query"},
        "keys exhausted",
    )

    assert route.call_count == 2
    assert result["results"] == [{
        "url": "https://good.example/page",
        "raw_content": "# Extracted Markdown",
        "images": [],
    }]
    assert len(result["failed_results"]) == 2
    assert result["_fallback"]["query_reranking_applied"] is False
    assert result["_fallback"]["cost_usd"]["total"] == 0.0


@respx.mock
async def test_extract_text_format_uses_jina_content(fallback: SearchFallback):
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


@respx.mock
async def test_extract_shares_total_content_budget_across_results(
    fallback: SearchFallback,
):
    respx.post("http://jina.test/process").mock(
        return_value=httpx.Response(200, json={"markdown": "x" * 100})
    )

    result = await fallback.extract(
        [f"https://source{index}.example" for index in range(3)],
        {"format": "markdown"},
        "keys exhausted",
    )

    assert [len(item["raw_content"]) for item in result["results"]] == [10, 10, 10]
    assert result["_fallback"]["total_content_limit_chars"] == 30
    assert result["_fallback"]["effective_per_result_limit_chars"] == 10
