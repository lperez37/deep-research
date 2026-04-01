# deep-research

<!-- Badges placeholder -->
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/protocol-MCP-purple)

Drop-in replacement for the official [Tavily MCP server](https://github.com/tavily-ai/tavily-mcp) that routes requests across multiple API keys to multiply your free-tier credits. Exposes search, extract, crawl, and map tools (research is excluded to conserve credits).

## What It Does

deep-research is a FastMCP server that exposes the core Tavily MCP tools (`tavily-search`, `tavily-extract`, `tavily-crawl`, `tavily-map`) with identical parameter schemas and response formats. The `tavily-research` endpoint is intentionally excluded because a single call can consume 15-250 credits. Behind the scenes, requests are distributed across N preconfigured Tavily API keys using round-robin rotation with credit-aware skipping, so clients never need to know that multiple keys exist. With two free-tier accounts (1,000 credits each), you get 2,000 credits per month instead of 1,000 -- swap one MCP config line and everything else stays the same.

## Architecture

```
+------------------------------------------------------+
|               Claude Desktop / Client                 |
|            (connects via stdio or HTTP)               |
+------------------------+-----------------------------+
                         | MCP Protocol
                         v
+------------------------------------------------------+
|            deep-research (FastMCP)                    |
|                                                      |
|  Tools:                                              |
|    tavily-search    --+                              |
|    tavily-extract     |   +--------------------+    |
|    tavily-crawl       +-->|   Key Router       |    |
|    tavily-map       --+   |                    |    |
|    credit-status          |  Strategy:         |    |
|                           |  - Credit-aware    |    |
|                           |  - Monthly reset   |    |
|                           +--------+-----------+    |
|                                    |                 |
|                    +---------------+----------+     |
|                    v               v          v     |
|               Key #1          Key #2  ...  Key #N   |
|              (1000 cr)       (1000 cr)   (1000 cr)  |
|                                                      |
|           Credit Tracker (SQLite)                    |
+------------------------+-----------------------------+
                         |
                         | HTTPS POST
                         v
                +-----------------+
                |  api.tavily.com |
                +-----------------+
```

## Quick Start

### Prerequisites

- Python 3.11 or later
- One or more [Tavily](https://tavily.com) accounts (free tier gives 1,000 credits/month each)

### Install

```bash
# Using pip
pip install -e .

# Or using uv (faster)
uv pip install -e .
```

### Configure

```bash
cp .env.example .env
```

Edit `.env` and add your Tavily API keys as a comma-separated list:

```
TAVILY_API_KEYS=tvly-key1,tvly-key2,tvly-key3,tvly-key4,tvly-key5
```

### Run

```bash
python -m deep_research
```

By default the server starts in **stdio** mode, ready for Claude Desktop or any MCP client that communicates over standard input/output.

To run in HTTP mode:

```bash
TRANSPORT=http python -m deep_research
```

The server will listen on `http://0.0.0.0:8000`.

## Docker

```bash
# Create .env with your keys
echo 'TAVILY_API_KEYS=tvly-key1,tvly-key2' > .env

# Start the server
docker compose up -d
```

The MCP endpoint is available at `http://your-host:8087/mcp`.

## Claude Code Integration

### 1. Remove the official Tavily MCP (if present)

```bash
# Check if you have it
claude mcp list

# Remove it (adjust scope if needed)
claude mcp remove tavily -s user
```

### 2. Add deep-research as a remote MCP server

```bash
claude mcp add tavily -s user -t http http://your-host:8087/mcp
```

Naming it `tavily` means your existing `mcp__tavily__*` permission rules
keep working and tools appear under the same namespace.

### 3. Auto-approve tools (optional)

Add to the `allow` list in `~/.claude/settings.json`:

```json
"mcp__tavily__tavily-search",
"mcp__tavily__tavily-extract",
"mcp__tavily__tavily-crawl",
"mcp__tavily__tavily-map",
"mcp__tavily__credit-status"
```

### 4. Verify

```bash
claude mcp list
```

You should see `tavily: http://your-host:8087/mcp (HTTP) - Connected`.

### Alternative: `.mcp.json` file

Instead of `claude mcp add`, you can create a `.mcp.json` in your project
root or home directory:

```json
{
  "mcpServers": {
    "tavily": {
      "type": "http",
      "url": "http://your-host:8087/mcp"
    }
  }
}
```

See [SETUP.md](SETUP.md) for full VPS deployment instructions and optional
bearer token authentication.

## Configuration Reference

All settings are read from environment variables. A `.env` file is supported via the `env_file` directive in Docker Compose or by loading it manually.

| Variable | Description | Default | Required |
|---|---|---|---|
| `TAVILY_API_KEYS` | Comma-separated list of Tavily API keys | -- | Yes |
| `CREDITS_PER_KEY` | Monthly credit budget per key | `1000` | No |
| `ROUTING_STRATEGY` | Key selection algorithm | `round-robin` | No |
| `DB_PATH` | Path to SQLite database for credit tracking | `/data/credits.db` | No |
| `TAVILY_BASE_URL` | Tavily REST API base URL | `https://api.tavily.com` | No |
| `TRANSPORT` | MCP transport mode: `stdio`, `http`, or `sse` | `stdio` | No |
| `HOST` | Listen address for HTTP/SSE transport | `0.0.0.0` | No |
| `PORT` | Listen port for HTTP/SSE transport | `8000` | No |
| `AUTH_TOKEN` | Bearer token to protect the MCP server (optional) | empty | No |

## Tools

deep-research exposes five MCP tools. The first four mirror the official Tavily MCP server exactly. The `tavily-research` endpoint is excluded because it costs 15-250 credits per call.

| Tool | Description | Credit Cost |
|---|---|---|
| `tavily-search` | Search the web for current information on any topic | 1 (basic) / 2 (advanced) |
| `tavily-extract` | Extract content from one or more URLs | 1 per 5 URLs (basic) / 2 per 5 URLs (advanced) |
| `tavily-crawl` | Crawl a website starting from a URL with configurable depth | 1 per 5 pages (basic) / 2 per 5 pages (advanced) |
| `tavily-map` | Map a website's URL structure | 1 per 10 pages / 2 per 10 pages (with instructions) |
| `credit-status` | Show remaining credits across all configured keys | 0 (local only) |

## Credit System

### How credits work

Each Tavily free-tier account receives 1,000 credits per month, resetting on the 1st. deep-research tracks estimated usage per key so that the router can skip exhausted keys and distribute load evenly.

### Estimation

Before each request, the server estimates the credit cost based on endpoint type and request parameters (search depth, URL count, page limits, model selection). This estimate is used for routing decisions. After the request completes, the actual credit count from the Tavily API response (`usage.credits`) is recorded when available, falling back to the estimate otherwise.

### SQLite tracking

Credit usage is persisted in a SQLite database (default: `/data/credits.db`). The schema stores `(key_id, period, used)` tuples where `period` is the current month in `YYYY-MM` format. WAL journal mode is enabled for safe concurrent access. When a new month begins, previous period rows are naturally ignored since queries filter by the current period -- no explicit reset is needed.

### Monthly reset

Credits reset automatically. The tracker queries only the current `YYYY-MM` period, so when the calendar month rolls over, all keys start fresh with zero recorded usage.

### Checking status

Use the `credit-status` tool to see remaining credits for each key. Keys are displayed in masked form (`tvly-abc...xyz1`) for security.

## Authentication

Authentication is optional and controlled by the `AUTH_TOKEN` environment variable.

**When `AUTH_TOKEN` is set**, the server registers a bearer token middleware. This is primarily relevant for HTTP/SSE transport where the server is network-accessible. For stdio transport, authentication is not enforced because the client already has local process access.

**When `AUTH_TOKEN` is empty or unset**, the server runs without authentication. This is the default and is suitable for local stdio usage.

To enable authentication:

```bash
AUTH_TOKEN=my-secret-token python -m deep_research
```

Or in `.env`:

```
AUTH_TOKEN=my-secret-token
```

## Development

### Install dev dependencies

```bash
pip install -e ".[dev]"
# or
uv pip install -e ".[dev]"
```

### Run tests

```bash
pytest
```

### Project structure

```
tavily-router/
├── deep_research/
│   ├── __init__.py          # Package version
│   ├── __main__.py          # python -m deep_research entrypoint
│   ├── server.py            # FastMCP server, tool definitions, routing glue
│   ├── router.py            # Round-robin key selection with credit-aware skipping
│   ├── credits.py           # SQLite credit tracker + cost estimation
│   ├── tavily_client.py     # Async HTTP client wrapping the Tavily REST API
│   └── config.py            # Pydantic settings from environment variables
├── tests/
│   ├── __init__.py
│   ├── test_credits.py       # Credit estimation and tracker tests
│   ├── test_router.py        # Round-robin routing and concurrency tests
│   └── test_tavily_client.py # HTTP client tests with respx mocking
├── Dockerfile               # Multi-stage build (builder + runtime)
├── docker-compose.yml       # stdio and HTTP service definitions
├── pyproject.toml           # Project metadata and dependencies
├── .env.example             # Template for environment configuration
├── plan.md                  # Design document and implementation plan
└── README.md
```

## How It Works

### Round-robin routing

When a tool is invoked, the server selects the next API key from a circular list. An asyncio lock ensures that concurrent requests do not select the same key simultaneously.

### Credit-aware skipping

Before forwarding a request, the router checks whether the selected key has enough estimated credit budget remaining for the current month. If a key is exhausted, it is skipped and the next key in rotation is tried. If all keys are exhausted, a clear error is returned.

### 429 retry

If Tavily returns HTTP 429 (rate limit or quota exceeded), the server marks that key as fully exhausted and retries the request with the next available key. Up to 3 retry attempts are made before surfacing the error to the client.

### Credit tracking

After each successful request, the actual credit cost (from the API response) is recorded in the SQLite database. This data drives future routing decisions and powers the `credit-status` tool.

### Drop-in compatibility

Tool names, parameter schemas, and response formats match the official Tavily MCP server. Switching from the official server to deep-research requires changing only the MCP server configuration -- no client code changes are needed.

## License

MIT
