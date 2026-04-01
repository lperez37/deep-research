# Deploying deep-research on a VPS

## On the VPS

```bash
ssh luis@vps
cd ~/apps
git clone git@github.com:lperez37/deep-research.git
cd deep-research
```

Create `.env` with your keys:

```bash
cat > .env << 'EOF'
TAVILY_API_KEYS=tvly-key1,tvly-key2
EOF
```

Start it:

```bash
docker compose up -d
```

The MCP server is now running at `http://vps:8087/mcp/`.

To check logs:

```bash
docker compose logs -f
```

## In Claude Code (your local machine)

```bash
claude mcp add deep-research --url http://vps:8087/mcp/
```

Done. All five tools are now available.

### Optional: protect with a bearer token

On the VPS, add `AUTH_TOKEN` to `.env`:

```
TAVILY_API_KEYS=tvly-key1,tvly-key2
AUTH_TOKEN=some-secret-token
```

Then restart and re-add with the token:

```bash
# VPS
docker compose restart

# Local
claude mcp remove deep-research
claude mcp add deep-research --url http://vps:8087/mcp/ \
  --header "Authorization: Bearer some-secret-token"
```

## Available Tools

| Tool | Credits |
|------|---------|
| `tavily-search` | 1 (basic) / 2 (advanced) |
| `tavily-extract` | 1 per 5 URLs |
| `tavily-crawl` | 1 per 5 pages |
| `tavily-map` | 1 per 10 pages |
| `credit-status` | 0 |
