# Deploying deep-research on a VPS

## On the VPS

```bash
ssh user@vps
cd ~/apps
git clone git@github.com:lperez37/deep-research.git
cd deep-research
```

Create `.env` with your Tavily API keys:

```bash
cat > .env << 'EOF'
TAVILY_API_KEYS=tvly-key1,tvly-key2
EOF
```

Start it:

```bash
docker compose up -d
```

Verify:

```bash
docker compose logs -f
# Should show: Uvicorn running on http://0.0.0.0:8087
```

The MCP endpoint is now at `http://vps:8087/mcp`.

## In Claude Code (your local machine)

### Step 1: Remove the official Tavily MCP

If you previously had the official Tavily MCP:

```bash
# Check current servers
claude mcp list

# Remove the old one
claude mcp remove tavily -s user
```

### Step 2: Add deep-research

```bash
claude mcp add tavily -s user -t http http://vps:8087/mcp
```

**Key flags:**
- `tavily` — the server name (keeps your existing `mcp__tavily__*` permissions working)
- `-s user` — available in all projects
- `-t http` — remote HTTP transport (required for URL-based servers)
- `http://vps:8087/mcp` — the MCP endpoint URL (no trailing slash)

### Step 3: Verify

```bash
claude mcp list
```

Expected output:

```
tavily: http://vps:8087/mcp (HTTP) - Connected
```

Then in a Claude Code session, try:

```
> use tavily-search to look up "FastMCP framework"
> check credit-status
```

### Step 4: Auto-approve tools (optional)

If you previously had `mcp__tavily__tavily-search` in your permissions, it
will keep working since the server is named `tavily`. To approve all tools,
add to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__tavily__tavily-search",
      "mcp__tavily__tavily-extract",
      "mcp__tavily__tavily-crawl",
      "mcp__tavily__tavily-map",
      "mcp__tavily__credit-status"
    ]
  }
}
```

## Alternative: `.mcp.json` file

Instead of `claude mcp add`, you can create a `.mcp.json` file. This is
useful for sharing config across machines or checking it into a project.

**User-level** (`~/.claude/.mcp.json` — all projects):

```json
{
  "mcpServers": {
    "tavily": {
      "type": "http",
      "url": "http://vps:8087/mcp"
    }
  }
}
```

**Project-level** (`.mcp.json` in project root — that project only):

```json
{
  "mcpServers": {
    "tavily": {
      "type": "http",
      "url": "http://vps:8087/mcp"
    }
  }
}
```

## Optional: Protect with a bearer token

On the VPS, add `AUTH_TOKEN` to `.env`:

```
TAVILY_API_KEYS=tvly-key1,tvly-key2
AUTH_TOKEN=some-secret-token
```

Restart:

```bash
docker compose restart
```

Then re-add with the header:

```bash
claude mcp remove tavily -s user
claude mcp add tavily -s user -t http \
  -H "Authorization: Bearer some-secret-token" \
  http://vps:8087/mcp
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "tavily": {
      "type": "http",
      "url": "http://vps:8087/mcp",
      "headers": {
        "Authorization": "Bearer some-secret-token"
      }
    }
  }
}
```

## Available Tools

| Tool | Credits |
|------|---------|
| `tavily-search` | 1 (basic) / 2 (advanced) |
| `tavily-extract` | 1 per 5 URLs |
| `tavily-crawl` | 1 per 5 pages |
| `tavily-map` | 1 per 10 pages |
| `credit-status` | 0 |

## Troubleshooting

### "Connection refused" or timeout

Check the container is running and the port is open:

```bash
ssh user@vps "docker compose -f ~/apps/deep-research/docker-compose.yml ps"
ssh user@vps "curl -s http://localhost:8087/mcp -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1.0\"}}}'"
```

### Check container logs

```bash
ssh user@vps "docker compose -f ~/apps/deep-research/docker-compose.yml logs --tail 20"
```

### "unknown option '--url'"

Use `-t http` before the URL:

```bash
# Wrong
claude mcp add tavily --url http://vps:8087/mcp

# Correct
claude mcp add tavily -t http http://vps:8087/mcp
```
