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

Credit usage is tracked in a SQLite database. Every response includes the remaining credit budget so you can see consumption in real time.

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

# Add your keys (comma-separated)
echo 'TAVILY_API_KEYS=tvly-key1,tvly-key2' > .env

# Start the server
docker compose up -d
```

The MCP endpoint is now at `http://your-host:8087/mcp`.

### 3. Connect from Claude Code

Remove the official Tavily MCP if you have it:

```bash
claude mcp remove tavily -s user
```

Add deep-research (naming it `tavily` keeps your existing permissions working):

```bash
claude mcp add tavily -s user -t http http://your-host:8087/mcp
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
      "url": "http://your-host:8087/mcp"
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

All settings are environment variables. Only `TAVILY_API_KEYS` is required.

| Variable | Default | Description |
|----------|---------|-------------|
| `TAVILY_API_KEYS` | -- | Comma-separated API keys |
| `CREDITS_PER_KEY` | `1000` | Monthly budget per key |
| `DB_PATH` | `/data/credits.db` | SQLite database path |
| `TRANSPORT` | `stdio` | `stdio`, `http`, or `sse` |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8000` | Listen port |
| `AUTH_TOKEN` | empty | Optional bearer token for HTTP auth |

## Authentication

By default, no auth is required. To protect the endpoint, set `AUTH_TOKEN` in `.env`:

```
AUTH_TOKEN=my-secret-token
```

Then connect with:

```bash
claude mcp add tavily -s user -t http \
  -H "Authorization: Bearer my-secret-token" \
  http://your-host:8087/mcp
```

## Development

```bash
pip install -e ".[dev]"
pytest                    # 63 tests, all passing
```

## License

MIT
