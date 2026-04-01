"""Unit tests for the TavilyClient async HTTP wrapper."""

from __future__ import annotations

import httpx
import pytest
import respx

from deep_research.tavily_client import TavilyClient, TavilyAPIError

BASE_URL = "https://api.tavily.com"
API_KEY = "tvly-test-key-1234"


@pytest.fixture
async def client():
    """Create a TavilyClient scoped to a single test, closed on teardown."""
    c = TavilyClient(base_url=BASE_URL)
    yield c
    await c.close()


# -------------------------------------------------------------------
# 1-5: Successful POST for each supported endpoint
# -------------------------------------------------------------------


@respx.mock
async def test_search_success(client: TavilyClient):
    """POST /search returns parsed JSON on 200."""
    payload = {"results": [{"title": "Example"}], "usage": {"credits": 1}}
    respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(200, json=payload),
    )

    result = await client.request("search", API_KEY, {"query": "test"})

    assert result == payload
    assert result["results"][0]["title"] == "Example"


@respx.mock
async def test_extract_success(client: TavilyClient):
    """POST /extract returns parsed JSON on 200."""
    payload = {"content": "extracted text", "usage": {"credits": 2}}
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(200, json=payload),
    )

    result = await client.request("extract", API_KEY, {"urls": ["https://example.com"]})

    assert result == payload
    assert result["content"] == "extracted text"


@respx.mock
async def test_crawl_success(client: TavilyClient):
    """POST /crawl returns parsed JSON on 200."""
    payload = {"pages": [{"url": "https://example.com", "content": "page"}]}
    respx.post(f"{BASE_URL}/crawl").mock(
        return_value=httpx.Response(200, json=payload),
    )

    result = await client.request("crawl", API_KEY, {"url": "https://example.com"})

    assert result == payload
    assert len(result["pages"]) == 1


@respx.mock
async def test_map_success(client: TavilyClient):
    """POST /map returns parsed JSON on 200."""
    payload = {"urls": ["https://example.com/a", "https://example.com/b"]}
    respx.post(f"{BASE_URL}/map").mock(
        return_value=httpx.Response(200, json=payload),
    )

    result = await client.request("map", API_KEY, {"url": "https://example.com"})

    assert result == payload
    assert len(result["urls"]) == 2


# -------------------------------------------------------------------
# 6: 401 raises TavilyAPIError with status 401
# -------------------------------------------------------------------


@respx.mock
async def test_401_raises_tavily_api_error(client: TavilyClient):
    """A 401 response raises TavilyAPIError with status_code 401."""
    respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"}),
    )

    with pytest.raises(TavilyAPIError) as exc_info:
        await client.request("search", "tvly-bad-key", {"query": "test"})

    assert exc_info.value.status_code == 401
    assert "Invalid API key" in exc_info.value.detail


# -------------------------------------------------------------------
# 7: 429 raises TavilyAPIError with status 429
# -------------------------------------------------------------------


@respx.mock
async def test_429_raises_tavily_api_error(client: TavilyClient):
    """A 429 response raises TavilyAPIError with status_code 429."""
    respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(429, json={"error": "Too Many Requests"}),
    )

    with pytest.raises(TavilyAPIError) as exc_info:
        await client.request("search", API_KEY, {"query": "test"})

    assert exc_info.value.status_code == 429
    assert "Rate limit" in exc_info.value.detail


# -------------------------------------------------------------------
# 8: Other HTTP errors (500) raise httpx.HTTPStatusError
# -------------------------------------------------------------------


@respx.mock
async def test_500_raises_http_status_error(client: TavilyClient):
    """A 500 response is not handled specially and raises HTTPStatusError."""
    respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.request("search", API_KEY, {"query": "test"})

    assert exc_info.value.response.status_code == 500


@respx.mock
async def test_502_raises_http_status_error(client: TavilyClient):
    """A 502 response also falls through to HTTPStatusError."""
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(502, text="Bad Gateway"),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.request("extract", API_KEY, {"urls": []})

    assert exc_info.value.response.status_code == 502


# -------------------------------------------------------------------
# 9: Unknown endpoint raises ValueError
# -------------------------------------------------------------------


async def test_unknown_endpoint_raises_value_error(client: TavilyClient):
    """An endpoint not in ENDPOINTS raises ValueError before any HTTP call."""
    with pytest.raises(ValueError, match="Unknown endpoint: foobar"):
        await client.request("foobar", API_KEY, {"query": "test"})


async def test_empty_endpoint_raises_value_error(client: TavilyClient):
    """An empty string endpoint raises ValueError."""
    with pytest.raises(ValueError, match="Unknown endpoint: "):
        await client.request("", API_KEY, {})


# -------------------------------------------------------------------
# 10: Authorization header is sent correctly
# -------------------------------------------------------------------


@respx.mock
async def test_request_sends_correct_authorization_header(client: TavilyClient):
    """The Authorization header is set to 'Bearer {api_key}'."""
    route = respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(200, json={"results": []}),
    )

    await client.request("search", API_KEY, {"query": "test"})

    assert route.called
    sent_request = route.calls.last.request
    assert sent_request.headers["authorization"] == f"Bearer {API_KEY}"


@respx.mock
async def test_different_api_key_is_reflected_in_header(client: TavilyClient):
    """Each call uses the api_key argument, not a cached value."""
    other_key = "tvly-other-key-5678"
    route = respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(200, json={"content": ""}),
    )

    await client.request("extract", other_key, {"urls": []})

    sent_request = route.calls.last.request
    assert sent_request.headers["authorization"] == f"Bearer {other_key}"


# -------------------------------------------------------------------
# 11: Request sends correct JSON body
# -------------------------------------------------------------------


@respx.mock
async def test_request_sends_correct_json_body(client: TavilyClient):
    """The JSON body matches the params dict passed to request()."""
    route = respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(200, json={"results": []}),
    )

    params = {"query": "climate change", "max_results": 5, "include_answer": True}
    await client.request("search", API_KEY, params)

    assert route.called
    import json

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == params


@respx.mock
async def test_empty_params_sends_empty_json_object(client: TavilyClient):
    """An empty params dict sends '{}' as the request body."""
    route = respx.post(f"{BASE_URL}/crawl").mock(
        return_value=httpx.Response(200, json={}),
    )

    await client.request("crawl", API_KEY, {})

    import json

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {}


# -------------------------------------------------------------------
# 12: close() method works
# -------------------------------------------------------------------


async def test_close_method():
    """Calling close() shuts down the underlying httpx.AsyncClient."""
    c = TavilyClient(base_url=BASE_URL)
    assert not c._http.is_closed

    await c.close()

    assert c._http.is_closed


async def test_close_is_idempotent():
    """Calling close() twice does not raise."""
    c = TavilyClient(base_url=BASE_URL)
    await c.close()
    # Second close should not raise
    await c.close()


# -------------------------------------------------------------------
# Additional edge cases
# -------------------------------------------------------------------


@respx.mock
async def test_tavily_api_error_string_representation():
    """TavilyAPIError __str__ includes both status code and detail."""
    err = TavilyAPIError(401, "Invalid API key")
    assert "401" in str(err)
    assert "Invalid API key" in str(err)


@respx.mock
async def test_all_endpoints_are_accepted(client: TavilyClient):
    """Every endpoint in ENDPOINTS is accepted without raising ValueError."""
    for endpoint in TavilyClient.ENDPOINTS:
        respx.post(f"{BASE_URL}/{endpoint}").mock(
            return_value=httpx.Response(200, json={}),
        )
        result = await client.request(endpoint, API_KEY, {})
        assert result == {}
