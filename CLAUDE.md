# CLAUDE.md

## Project Overview

**deep-research** is a FastMCP-based MCP server that acts as a drop-in replacement
for the official Tavily MCP server. It routes requests across multiple Tavily API
keys to multiply free-tier credit allowances (1,000 credits/key/month).

## Architecture

- `deep_research/config.py` — Pydantic settings from env vars
- `deep_research/credits.py` — SQLite credit tracker (WAL mode) + cost estimation
- `deep_research/router.py` — Round-robin key selection with credit-aware skipping
- `deep_research/tavily_client.py` — Async httpx wrapper for Tavily REST API
- `deep_research/server.py` — FastMCP server with 5 tools (4 Tavily + credit-status)

## Quick Commands

```bash
# Install
pip install -e ".[dev]"

# Run tests (63 tests, all should pass)
pytest

# Run server (stdio mode)
TAVILY_API_KEYS=key1,key2 python -m deep_research

# Run server (HTTP mode)
TAVILY_API_KEYS=key1,key2 TRANSPORT=http python -m deep_research

# Docker (HTTP on :8087)
docker compose up -d
```

## Deployment

Production runs in Docker on `vps` at `~/apps/deep-research`, exposing
`http://vps:8087/mcp`. Container name: `deep-research-deep-research-1`.

### Code changes — commit/push locally, then pull/deploy on VPS

```bash
# 1. Locally: commit and push to origin/main
git add <files> && git commit -m "..." && git push

# 2. On VPS: pull and restart
ssh luis@vps "cd ~/apps/deep-research && git pull && docker compose up -d --build"

# 3. Verify
ssh luis@vps "docker compose -f ~/apps/deep-research/docker-compose.yml ps"
ssh luis@vps "docker logs deep-research-deep-research-1 2>&1 | grep 'Starting deep-research'"
```

### Config changes — edit `.env` directly on VPS

`.env` is gitignored, so secrets (Tavily keys, `AUTH_TOKEN`) are not in git.
Mirror changes by hand:

```bash
# 1. Edit local .env (for stdio/dev use)
# 2. Edit VPS .env and recreate container so env_file is re-read
ssh luis@vps "vi ~/apps/deep-research/.env"
ssh luis@vps "cd ~/apps/deep-research && docker compose up -d --force-recreate"

# 3. Verify the new key count / values were picked up
ssh luis@vps "docker logs deep-research-deep-research-1 2>&1 | grep 'Starting deep-research'"
# Or call credit-status via MCP to see all keys live (see SETUP.md troubleshooting)
```

`docker compose restart` alone does NOT re-read `env_file` — use
`up -d --force-recreate` after editing `.env`.

## Tech Stack

- **Python 3.11+**
- **FastMCP 3.x** — MCP server framework
- **httpx** — async HTTP client
- **SQLite** (WAL mode) — credit tracking persistence
- **pydantic-settings** — environment variable configuration
- **pytest + respx** — testing with HTTP mocking

## Key Design Decisions

1. **Custom tools, not proxy**: Tools are defined with `@mcp.tool` and make direct
   HTTP calls to the Tavily REST API. This gives full control over key injection
   without running the Tavily MCP as a subprocess.

2. **SQLite over JSON**: Credit tracking uses SQLite with WAL journal mode for
   crash-safe, concurrent-safe persistence. Monthly reset is implicit — queries
   filter by `YYYY-MM` period.

3. **No auth by default**: The `AUTH_TOKEN` env var is optional. When set, a bearer
   token middleware is registered. Stdio transport skips auth since the client
   already has local process access.

4. **429 retry with key rotation**: On rate limit errors, the current key is
   force-exhausted and the request retries with the next available key (up to 3
   attempts).

## Environment Variables

| Variable | Required | Default |
|----------|----------|---------|
| `TAVILY_API_KEYS` | Yes | — |
| `CREDITS_PER_KEY` | No | `1000` |
| `DB_PATH` | No | `/data/credits.db` |
| `TRANSPORT` | No | `stdio` |
| `HOST` | No | `0.0.0.0` |
| `PORT` | No | `8000` |
| `AUTH_TOKEN` | No | empty (no auth) |

## Testing

Tests use in-memory SQLite and `respx` for HTTP mocking — no network or Tavily
account needed. Run with `pytest -v` for verbose output.

## File Size Guidelines

- Source files are 30–200 lines each (intentionally small and focused)
- No file exceeds 400 lines
