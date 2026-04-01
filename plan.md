# deep-research — Tavily Multi-Key MCP Gateway

## Overview

A FastMCP-based MCP server that acts as a **drop-in replacement** for the official
[Tavily MCP server](https://github.com/tavily-ai/tavily-mcp). It exposes identical
tools (`tavily-search`, `tavily-extract`, `tavily-crawl`, `tavily-map`,
`tavily-research`) but routes each request to one of **5 preconfigured Tavily API
keys** in the background, effectively multiplying the free-tier allowance from
1,000 to **5,000 credits/month**.

---

## Research Summary

### Tavily API (REST)

| Endpoint | Method | Base URL | Auth |
|----------|--------|----------|------|
| Search | POST | `https://api.tavily.com/search` | `Bearer tvly-...` |
| Extract | POST | `https://api.tavily.com/extract` | `Bearer tvly-...` |
| Crawl | POST | `https://api.tavily.com/crawl` | `Bearer tvly-...` |
| Map | POST | `https://api.tavily.com/map` | `Bearer tvly-...` |
| Research | POST | `https://api.tavily.com/research` | `Bearer tvly-...` |

### Credit Costs Per Call

| Endpoint | Depth | Cost |
|----------|-------|------|
| Search | basic / fast / ultra-fast | 1 credit |
| Search | advanced | 2 credits |
| Extract | basic | 1 credit per 5 successful URLs |
| Extract | advanced | 2 credits per 5 successful URLs |
| Map | without instructions | 1 credit per 10 pages |
| Map | with instructions | 2 credits per 10 pages |
| Crawl | basic extraction | 1 credit per 5 pages |
| Crawl | advanced extraction | 2 credits per 5 pages |
| Research | model=mini | 4–110 credits |
| Research | model=pro | 15–250 credits |

### Free Tier

- **1,000 credits/month** per account (Researcher plan, no credit card)
- Credits reset on the 1st of each month
- Rate limit: 100 RPM (Dev), 1000 RPM (Prod)
- 5 accounts × 1,000 = **5,000 credits/month**

### FastMCP (v2/v3)

- `@mcp.tool` decorator auto-generates JSON Schema from Python type hints
- Custom tool name via `@mcp.tool(name="tavily-search")`
- Async tools supported (`async def`) — use `httpx.AsyncClient` for HTTP calls
- Transports: `stdio` (default for Claude Desktop), `http` (Streamable HTTP), `sse`
- Middleware pipeline for logging, auth, rate limiting
- Docker-friendly: runs via `fastmcp run` CLI or `mcp.run()` in Python
- Proxy pattern exists but **not needed here** — we build custom tools that call the
  Tavily REST API directly, giving us full control over key routing

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Claude Desktop / Client             │
│              (connects via stdio or HTTP)              │
└──────────────────────┬───────────────────────────────┘
                       │ MCP Protocol
                       ▼
┌──────────────────────────────────────────────────────┐
│              deep-research (FastMCP)                   │
│                                                        │
│  Tools:                                                │
│    tavily-search    ─┐                                 │
│    tavily-extract    │   ┌──────────────────────┐     │
│    tavily-crawl      ├──▶│   Key Router          │     │
│    tavily-map        │   │                      │     │
│    tavily-research  ─┘   │  Strategy:           │     │
│                          │  - Round-robin        │     │
│                          │  - Credit-aware skip  │     │
│                          │  - Monthly reset      │     │
│                          └──────┬───────────────┘     │
│                                 │                       │
│                    ┌────────────┼────────────┐         │
│                    ▼            ▼            ▼         │
│               Key #1       Key #2  ...  Key #5        │
│              (1000 cr)    (1000 cr)    (1000 cr)       │
│                                                        │
│            Credit Tracker (SQLite / in-memory)         │
└──────────────────────────────────────────────────────┘
                       │
                       │ HTTPS POST
                       ▼
              ┌─────────────────┐
              │  api.tavily.com │
              └─────────────────┘
```

### Key Design Decisions

1. **Custom tools, not proxy**: We define each tool manually with `@mcp.tool` rather
   than using FastMCP's `create_proxy()`. This gives full control over which API key
   is injected into each outgoing request without needing to run the Tavily MCP as a
   subprocess.

2. **Round-robin with credit awareness**: Simple round-robin across keys, but skip
   any key that has exhausted its estimated monthly budget. Estimated cost is computed
   from the request parameters (search depth, URL count, etc.) before sending.

3. **Credit tracking**: Lightweight JSON file (`/data/credits.json`) persisted to a
   Docker volume. Tracks `{key_id: {used: N, reset_date: "YYYY-MM-01"}}`.
   Auto-resets on the 1st of each month.

4. **Drop-in replacement**: Tool names, parameter schemas, and response formats match
   the official Tavily MCP server exactly. Clients swap one MCP config for another
   with zero code changes.

5. **Docker Compose**: Single service with environment variables for the 5 API keys,
   persistent volume for credit tracking, stdio or HTTP transport.

---

## Implementation Plan

### Phase 1: Project Scaffold

```
tavily-router/
├── deep_research/
│   ├── __init__.py
│   ├── server.py          # FastMCP server + tool definitions
│   ├── router.py          # Key rotation + credit-aware routing
│   ├── credits.py         # Credit estimation + tracking + persistence
│   ├── tavily_client.py   # Async HTTP client wrapping Tavily REST API
│   └── config.py          # Settings via environment variables
├── tests/
│   ├── test_router.py
│   ├── test_credits.py
│   ├── test_tavily_client.py
│   └── test_server.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── README.md
└── plan.md
```

**Dependencies:**
- `fastmcp>=2.0` — MCP server framework
- `httpx` — async HTTP client for Tavily REST API calls
- `pydantic` or `pydantic-settings` — config/env parsing
- `pytest`, `pytest-asyncio`, `respx` — testing

### Phase 2: Configuration (`config.py`)

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # 5 Tavily API keys (free-tier accounts)
    tavily_api_keys: list[str]  # comma-separated in env: TAVILY_API_KEYS

    # Monthly credit limit per key (default: free tier)
    credits_per_key: int = 1000

    # Routing strategy
    routing_strategy: str = "round-robin"  # "round-robin" | "least-used" | "random"

    # Credit tracking persistence
    credit_file: str = "/data/credits.json"

    # Server transport
    transport: str = "stdio"  # "stdio" | "http" | "sse"
    host: str = "0.0.0.0"
    port: int = 8000

    # Tavily API base URL
    tavily_base_url: str = "https://api.tavily.com"

    model_config = {"env_prefix": "", "env_nested_delimiter": "__"}
```

**Environment variables:**
```env
TAVILY_API_KEYS=tvly-key1,tvly-key2,tvly-key3,tvly-key4,tvly-key5
CREDITS_PER_KEY=1000
ROUTING_STRATEGY=round-robin
CREDIT_FILE=/data/credits.json
TRANSPORT=stdio
```

### Phase 3: Key Router (`router.py`)

Responsibilities:
- Maintain ordered list of API keys
- Track current index for round-robin
- Skip keys that have hit their monthly limit
- Thread-safe (asyncio lock)
- Expose `async def get_key() -> str` and `async def report_usage(key, credits)`

```python
import asyncio
from datetime import datetime, timezone

class KeyRouter:
    def __init__(self, keys: list[str], credits_per_key: int, credit_tracker):
        self._keys = keys
        self._credits_per_key = credits_per_key
        self._tracker = credit_tracker
        self._index = 0
        self._lock = asyncio.Lock()

    async def get_key(self) -> str:
        """Return next available key via round-robin, skipping exhausted keys."""
        async with self._lock:
            for _ in range(len(self._keys)):
                key = self._keys[self._index]
                self._index = (self._index + 1) % len(self._keys)
                used = await self._tracker.get_usage(key)
                if used < self._credits_per_key:
                    return key
            raise RuntimeError("All API keys exhausted for this billing period")

    async def report_usage(self, key: str, credits: int):
        """Record credit usage for a key."""
        await self._tracker.add_usage(key, credits)
```

### Phase 4: Credit Tracker (`credits.py`)

Responsibilities:
- Persist `{key: {used: int, period: "YYYY-MM"}}` to JSON file
- Auto-reset when month changes
- Estimate credits before request (for pre-routing) and update after response
- Credit estimation logic per endpoint:

```python
def estimate_credits(endpoint: str, params: dict) -> int:
    """Estimate credit cost of a request before sending."""
    match endpoint:
        case "search":
            return 2 if params.get("search_depth") == "advanced" else 1
        case "extract":
            url_count = len(params.get("urls", []))
            multiplier = 2 if params.get("extract_depth") == "advanced" else 1
            return max(1, (url_count // 5) * multiplier)
        case "map":
            base = 2 if params.get("instructions") else 1
            pages = params.get("limit", 50)
            return max(1, (pages // 10) * base)
        case "crawl":
            multiplier = 2 if params.get("extract_depth") == "advanced" else 1
            pages = params.get("limit", 50)
            return max(1, (pages // 5) * multiplier)
        case "research":
            # Use midpoint estimates
            return 60 if params.get("model") == "pro" else 30
        case _:
            return 1
```

### Phase 5: Tavily HTTP Client (`tavily_client.py`)

Thin async wrapper around Tavily REST API:

```python
import httpx

class TavilyClient:
    def __init__(self, base_url: str = "https://api.tavily.com"):
        self._base_url = base_url
        self._http = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    async def request(self, endpoint: str, api_key: str, params: dict) -> dict:
        """Make authenticated POST request to Tavily API."""
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await self._http.post(
            f"/{endpoint}",
            json=params,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._http.aclose()
```

### Phase 6: MCP Server (`server.py`)

The core — FastMCP server with tools matching the official Tavily MCP exactly.

```python
from fastmcp import FastMCP
from typing import Optional

mcp = FastMCP(name="deep-research")

# ── tavily-search ──────────────────────────────────────────────

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
    """Search the web using Tavily's AI search engine."""
    params = {k: v for k, v in locals().items() if v is not None}
    return await _route_request("search", params)

# ── tavily-extract ─────────────────────────────────────────────

@mcp.tool(name="tavily-extract")
async def tavily_extract(
    urls: list[str],
    extract_depth: str = "basic",
    include_images: bool = False,
    format: str = "markdown",
    include_favicon: bool = False,
) -> dict:
    """Extract content from URLs using Tavily."""
    params = {k: v for k, v in locals().items() if v is not None}
    return await _route_request("extract", params)

# ── tavily-crawl ───────────────────────────────────────────────

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
    """Crawl a website starting from a URL using Tavily."""
    params = {k: v for k, v in locals().items() if v is not None}
    return await _route_request("crawl", params)

# ── tavily-map ─────────────────────────────────────────────────

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
    """Map a website's URL structure using Tavily."""
    params = {k: v for k, v in locals().items() if v is not None}
    return await _route_request("map", params)

# ── tavily-research ────────────────────────────────────────────

@mcp.tool(name="tavily-research")
async def tavily_research(
    input: str,
    model: str = "auto",
) -> dict:
    """Perform comprehensive research on a topic using Tavily."""
    params = {k: v for k, v in locals().items() if v is not None}
    return await _route_request("research", params)

# ── Internal routing ───────────────────────────────────────────

async def _route_request(endpoint: str, params: dict) -> dict:
    """Pick a key, send the request, track credits."""
    estimated = estimate_credits(endpoint, params)
    key = await router.get_key()
    try:
        result = await tavily_client.request(endpoint, key, params)
        # Use actual credits from response if available, else estimate
        actual = result.get("usage", {}).get("credits", estimated)
        await router.report_usage(key, actual)
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            # Key exhausted — mark and retry with next key
            await router.report_usage(key, credits_per_key)  # force exhaust
            return await _route_request(endpoint, params)  # retry once
        raise
```

### Phase 7: Docker Setup

**`Dockerfile`:**
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency resolution
RUN pip install uv

# Copy project files
COPY pyproject.toml .
RUN uv pip install --system -e ".[dev]" || uv pip install --system .

COPY . .

# Credit tracking volume mount point
RUN mkdir -p /data

EXPOSE 8000

# Default: stdio transport (override via TRANSPORT env var)
CMD ["python", "-m", "deep_research.server"]
```

**`docker-compose.yml`:**
```yaml
services:
  deep-research:
    build: .
    container_name: deep-research
    environment:
      - TAVILY_API_KEYS=tvly-key1,tvly-key2,tvly-key3,tvly-key4,tvly-key5
      - CREDITS_PER_KEY=1000
      - ROUTING_STRATEGY=round-robin
      - CREDIT_FILE=/data/credits.json
      - TRANSPORT=stdio
    volumes:
      - credit-data:/data
    # For HTTP transport, uncomment:
    # ports:
    #   - "8000:8000"
    # environment:
    #   - TRANSPORT=http
    stdin_open: true   # Required for stdio transport
    tty: true          # Required for stdio transport

volumes:
  credit-data:
```

### Phase 8: Client Configuration (Drop-in Replacement)

**Before (official Tavily MCP in Claude Desktop):**
```json
{
  "mcpServers": {
    "tavily": {
      "command": "npx",
      "args": ["-y", "tavily-mcp@latest"],
      "env": {
        "TAVILY_API_KEY": "tvly-single-key"
      }
    }
  }
}
```

**After (deep-research — stdio via Docker):**
```json
{
  "mcpServers": {
    "tavily": {
      "command": "docker",
      "args": [
        "compose", "-f", "/path/to/tavily-router/docker-compose.yml",
        "run", "--rm", "-i", "deep-research"
      ]
    }
  }
}
```

**After (deep-research — stdio via Python directly):**
```json
{
  "mcpServers": {
    "tavily": {
      "command": "python",
      "args": ["-m", "deep_research.server"],
      "env": {
        "TAVILY_API_KEYS": "tvly-key1,tvly-key2,tvly-key3,tvly-key4,tvly-key5"
      }
    }
  }
}
```

**After (deep-research — HTTP transport via Docker Compose):**
```json
{
  "mcpServers": {
    "tavily": {
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

---

## Implementation Steps (ordered)

| # | Task | Files | Depends On |
|---|------|-------|------------|
| 1 | Project scaffold: `pyproject.toml`, package structure, `__init__.py` | `pyproject.toml`, `deep_research/__init__.py` | — |
| 2 | Configuration module with env var parsing | `deep_research/config.py` | 1 |
| 3 | Credit estimation + tracking (with tests) | `deep_research/credits.py`, `tests/test_credits.py` | 2 |
| 4 | Key router with round-robin + credit awareness (with tests) | `deep_research/router.py`, `tests/test_router.py` | 3 |
| 5 | Async Tavily HTTP client wrapper (with tests) | `deep_research/tavily_client.py`, `tests/test_tavily_client.py` | 2 |
| 6 | FastMCP server with all 5 tool definitions | `deep_research/server.py` | 2, 4, 5 |
| 7 | Integration tests (mock Tavily API with `respx`) | `tests/test_server.py` | 6 |
| 8 | Dockerfile | `Dockerfile` | 6 |
| 9 | Docker Compose config | `docker-compose.yml` | 8 |
| 10 | Documentation | `README.md` | 9 |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Tavily detects multi-account usage and blocks keys | Keys are used from same IP but different accounts; free tier ToS doesn't explicitly prohibit this for personal use. Keep usage reasonable. |
| Credit estimation inaccuracy | Use actual `usage.credits` from API response when available; estimation is only for pre-routing decisions. |
| All 5 keys exhausted mid-month | Return clear error message with credit status. Consider adding a `credit-status` tool for visibility. |
| Tavily API changes endpoints/schemas | Pin to known API behavior; extract endpoint URLs to config for easy updates. |
| Research endpoint is async (returns task ID) | Handle polling: POST to create task, then GET to poll for completion. Match Tavily MCP behavior. |
| Rate limiting (429) across keys | On 429, mark key as temporarily exhausted and rotate to next key with exponential backoff. |

---

## Bonus Features (post-MVP)

- [ ] `credit-status` tool — returns remaining credits per key
- [ ] Dashboard web UI — simple HTML page showing credit usage
- [ ] Webhook/notification when credits are low
- [ ] Support for paid keys mixed with free keys (different limits per key)
- [ ] Prometheus metrics endpoint for monitoring
- [ ] Automatic account health check on startup (validate all keys)
