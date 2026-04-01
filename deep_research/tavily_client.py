"""Thin async HTTP wrapper around the Tavily REST API."""

from __future__ import annotations

import httpx


class TavilyAPIError(Exception):
    """Raised when the Tavily API returns a non-success status."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Tavily API {status_code}: {detail}")


class TavilyClient:
    """Async client for Tavily REST endpoints."""

    ENDPOINTS = frozenset({"search", "extract", "crawl", "map"})

    def __init__(self, base_url: str = "https://api.tavily.com") -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def request(
        self, endpoint: str, api_key: str, params: dict
    ) -> dict:
        """POST to ``/{endpoint}`` with bearer auth and return JSON."""
        if endpoint not in self.ENDPOINTS:
            raise ValueError(f"Unknown endpoint: {endpoint}")

        response = await self._http.post(
            f"/{endpoint}",
            json=params,
            headers={"Authorization": f"Bearer {api_key}"},
        )

        if response.status_code == 401:
            raise TavilyAPIError(401, "Invalid API key")
        if response.status_code == 429:
            raise TavilyAPIError(429, "Rate limit or credit quota exceeded")
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self._http.aclose()
