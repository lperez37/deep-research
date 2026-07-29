# deep-research

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/protocol-MCP-purple)

Free Tavily for personal use.

## Why

Tavily gives you 1,000 API credits per month on their free tier. That runs out fast when AI agents are making search calls on your behalf. Paid plans start at $30/month.

This project solves that by running a lightweight gateway that spreads requests across multiple free-tier Tavily accounts. Two accounts give you 2,000 credits/month. Five give you 5,000. You get the same Tavily tools with the same parameters and the same response format, but your credits last much longer.

The `tavily-research` endpoint is deliberately excluded. A single research call can burn 15 to 250 credits, which would drain your free budget in a handful of requests. The four remaining tools (search, extract, crawl, map) cost 1-2 credits each and cover the vast majority of use cases.

## How it works

deep-research is a [FastMCP](https://github.com/jlowin/fastmcp) server that exposes the same MCP tools as the official Tavily MCP server. When a tool is called, it picks the next API key from a round-robin rotation, skipping any key that has used up its monthly budget. If Tavily returns a 429 (rate limit), the key is marked as exhausted and the request is retried with the next key.

Credit usage and attributed request events are tracked in SQLite. Every response includes the remaining credit budget so you can see consumption in real time.

An optional fallback can keep search and extraction available after every
Tavily key is exhausted or cooling down. For search, it takes the top 3
Amsterdam-localized Google organic results from DataForSEO, fetches those pages
concurrently as Markdown through a Jina Reader proxy, then uses a fast LLM to
produce a compact cross-source answer and per-source summaries with structured
entity metadata. Raw page Markdown is never returned by fallback search, even
when requested. Fallback extraction uses the same synthesis step instead of
returning page Markdown: each URL gets at most a 300-character summary, with a
6,000-character aggregate summary budget. Fallback responses identify
themselves and include a cost breakdown.

The fallback location is always Amsterdam. A `country` requested by the client
is ignored only during fallback and is echoed in fallback metadata for clarity.

```
Client (Claude Code, etc.)
    |
    v
deep-research gateway (port 8087)
    |
    |-- round-robin key selection
    |-- credit tracking (SQLite)
    |-- 429 retry with key rotation
    |
    v
api.tavily.com
```

## Quick start

### 1. Get Tavily API keys

Create one or more free accounts at [tavily.com](https://tavily.com). Each gives you 1,000 credits/month.

### 2. Deploy with Docker

```bash
git clone https://github.com/lperez37/deep-research.git
cd deep-research

# Add your keys and generate a bearer token. Save the printed token for clients.
AUTH_TOKEN="$(openssl rand -hex 32)"
printf 'TAVILY_API_KEYS=tvly-key1,tvly-key2\nAUTH_TOKEN=%s\n' "$AUTH_TOKEN" > .env
printf 'Bearer token: %s\n' "$AUTH_TOKEN"

# Start the server
docker compose up -d
```

The MCP endpoint is now at `http://your-host:8087/mcp`.

Use plain HTTP only across an encrypted private network such as Tailscale. Put
the service behind an HTTPS reverse proxy before exposing it to the public
internet; bearer tokens and search contents must not cross an untrusted network
in plaintext.

### 3. Connect from Claude Code

Remove the official Tavily MCP if you have it:

```bash
claude mcp remove tavily -s user
```

Add deep-research (naming it `tavily` keeps your existing permissions working):

```bash
claude mcp add tavily -s user -t http \
  -H "Authorization: Bearer YOUR_SAVED_AUTH_TOKEN" \
  http://your-host:8087/mcp
```

Verify:

```bash
claude mcp list
# tavily: http://your-host:8087/mcp (HTTP) - Connected
```

### Alternative: `.mcp.json`

```json
{
  "mcpServers": {
    "tavily": {
      "type": "http",
      "url": "http://your-host:8087/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SAVED_AUTH_TOKEN"
      }
    }
  }
}
```

## Tools

| Tool | What it does | Cost |
|------|-------------|------|
| `tavily-search` | Web search with snippets and source URLs | 1 credit (basic) / 2 (advanced) |
| `tavily-extract` | Extract page content from URLs | 1 per 5 URLs |
| `tavily-crawl` | Crawl a site with configurable depth | 1 per 5 pages |
| `tavily-map` | Discover a site's URL structure | 1 per 10 pages |
| `credit-status` | Check remaining credits per key | free |

Every response includes a `_credits_remaining` field like `"1942/2000 credits remaining (97.1%)"`.

## Configuration

All settings are environment variables. `TAVILY_API_KEYS` is always required.
`AUTH_TOKEN` is also required for HTTP and SSE unless the explicit development
override is enabled.

| Variable | Default | Description |
|----------|---------|-------------|
| `TAVILY_API_KEYS` | -- | Comma-separated API keys |
| `CREDITS_PER_KEY` | `1000` | Monthly budget per key |
| `DB_PATH` | `/data/credits.db` | SQLite database path |
| `TRANSPORT` | `stdio` | `stdio`, `http`, or `sse` |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8000` | Listen port |
| `AUTH_TOKEN` | empty | Required bearer token for HTTP and SSE |
| `ALLOW_UNAUTHENTICATED_HTTP` | `false` | Explicit development-only network auth override |
| `TRUST_PROXY_HEADERS` | `false` | Trust the first `X-Forwarded-For` address for audit attribution. Enable only behind a trusted proxy |
| `AUDIT_MAX_TEXT_CHARS` | `8192` | Maximum persisted query or instruction length |
| `AUDIT_RETENTION_DAYS` | `90` | Automatic completed-row retention |
| `AUDIT_BUSY_TIMEOUT_SECONDS` | `0.1` | Maximum SQLite lock wait for request audit writes |
| `SESSION_BURST_LIMIT` | `5` | Metered calls allowed per MCP session window (`0` disables) |
| `SESSION_BURST_WINDOW_SECONDS` | `60` | Rolling session burst window in seconds |
| `FALLBACK_ENABLED` | `false` | Enable the search/extract exhaustion fallback |
| `DATAFORSEO_AUTH` | empty | Base64 DataForSEO `login:password` value |
| `DATAFORSEO_LOCATION_NAME` | `Amsterdam,North Holland,Netherlands` | Google SERP location |
| `FALLBACK_LLM_BASE_URL` | `https://router.vivacityholding.com/v1` | OpenAI-compatible synthesis URL |
| `FALLBACK_LLM_API_KEY` | empty | Synthesis API key |
| `FALLBACK_LLM_MODEL` | `deepseek-v4-flash` | Search-synthesis model |
| `JINA_SCRAPER_BASE_URL` | `http://100.119.183.110:9567` | Jina Reader proxy URL |
| `FALLBACK_CONTENT_MAX_CHARS` | `12000` | Maximum explicit raw/extract characters per source |
| `FALLBACK_SEARCH_CONTENT_MAX_CHARS` | `1500` | Maximum Jina Markdown characters sent to synthesis per source |
| `FALLBACK_EXTRACT_TOTAL_MAX_CHARS` | `6000` | Aggregate Jina content budget shared across extract synthesis inputs |
| `FALLBACK_DAILY_COST_LIMIT_USD` | `1.00` | Durable UTC daily fallback spending ceiling |
| `FALLBACK_MAX_CONCURRENCY` | `2` | Maximum simultaneous fallback pipelines |
| `FALLBACK_MAX_COST_PER_SEARCH_USD` | `0.02` | Reserved upper cost bound per search |
| `JINA_MAX_RESPONSE_BYTES` | `1100000` | Hard router-side Jina response byte limit |
| `BIND_HOST` | `127.0.0.1` | Docker-published host address; use a Tailnet IP remotely |

## Request audit log

The gateway attempts to write every Tavily tool call to the `request_log` table
in the same SQLite database as credit usage. Logging starts before the upstream
request so failures and server-side cancellations remain visible. The completion
update records status, credits, retry count and duration. Audit writes are deliberately
best-effort: a logging failure is emitted to the server log but does not make web
search unavailable.

The log contains:

- UTC timestamps, MCP request ID, session ID and transport
- endpoint, bounded search/extract query or crawl/map instructions and sanitised URL target
- requester ID, hostname, application and application version
- source IP and user agent for HTTP requests
- success, failure, cancellation or abandoned status, Tavily credits, attempts, duration and a bounded error code
- fallback provider, reported USD cost, returned-item count and failed-item count

An HTTP client disconnect does not necessarily cancel work already accepted by
FastMCP. If Tavily completes after a disconnect, the row records the upstream
outcome rather than claiming that the server coroutine was cancelled.

MCP client name and version are used as the application fallback. HTTP clients
can supply more useful attribution with these headers:

```text
X-Requester-ID: research-team
X-Requester-Hostname: agent-host-3
X-Requester-Application: Hermes
X-Requester-Application-Version: 1.0
```

These headers are self-declared labels, not authenticated identity claims. Raw
headers and credentials are never stored. `X-Forwarded-For` is ignored unless
`TRUST_PROXY_HEADERS=true` because it is otherwise spoofable. For stdio, the
local hostname and MCP client information are recorded automatically.

The server rejects a sixth metered Tavily call from the same MCP session within
60 seconds by default. This circuit breaker prevents accidental agent fan-out
before additional credits are spent. Adjust the two `SESSION_BURST_*` settings
only when another gateway already enforces a research budget.

Stored URL targets retain scheme, host and port plus a non-reversible path
fingerprint. User information, path content, query strings and fragments are
removed. Malformed targets are recorded as `[invalid URL]`. Queries and
instructions are capped at `AUDIT_MAX_TEXT_CHARS` characters. Truncated values
include a SHA-256 fingerprint of the complete input. The SQLite database and
live WAL sidecars are restricted to owner access (`0600`).

`credit-status` includes completed fallback counts for today and the current
month, grouped by tool, plus reported fallback cost and today's failed fallback
attempt count.

Queries and source IP addresses can contain personal or confidential data. The
audit log is therefore not exposed as an MCP tool. Inspect it only through
controlled database access and apply retention appropriate to your environment:

```bash
# Inspect the named Compose volume through the shipped Python runtime.
docker compose exec deep-research /app/.venv/bin/python -c \
  "import json,sqlite3; c=sqlite3.connect('/data/credits.db'); print(json.dumps(c.execute('SELECT created_at, application, hostname, endpoint, query, status FROM request_log ORDER BY id DESC LIMIT 20').fetchall(), indent=2))"

# Example 90-day policy. Run VACUUM separately if space must be reclaimed.
docker compose exec deep-research /app/.venv/bin/python -c \
  "import sqlite3; c=sqlite3.connect('/data/credits.db'); n=c.execute(\"DELETE FROM request_log WHERE julianday(created_at) < julianday('now', '-90 days')\").rowcount; c.commit(); print(f'deleted {n} rows')"
```

Completed rows older than `AUDIT_RETENTION_DAYS` are deleted on startup and by
daily maintenance while the service remains running, with a default retention
of 90 days. Rows left in `started` for more than 24 hours are classified as
`abandoned` before retention is applied.

## Authentication

HTTP and SSE transports require `AUTH_TOKEN` by default. Stdio remains
unauthenticated because it is a local process transport. Set the token in `.env`:

```
AUTH_TOKEN=my-secret-token
```

An isolated development deployment can explicitly opt out with
`ALLOW_UNAUTHENTICATED_HTTP=true`. Do not use that override on a published port.

Then connect with:

```bash
claude mcp add tavily -s user -t http \
  -H "Authorization: Bearer my-secret-token" \
  http://your-host:8087/mcp
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
