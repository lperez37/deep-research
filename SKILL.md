---
name: tavily-router
description: |
  Search the web, extract page content, crawl sites, and map URLs via the
  Tavily MCP gateway (deep-research). Routes requests across multiple API keys
  with credit-aware load balancing. Use when the user needs web search, content
  extraction, site crawling, URL discovery, or says "search for", "look up",
  "find", "extract from", "crawl", "map site", or "check credits".
allowed-tools: mcp__tavily__*(*)
---

# Tavily Router

Multi-key Tavily MCP gateway running at `http://vps:8087/mcp`. Every response
includes a `_credits_remaining` field showing the current budget.

## Tools

### tavily-search

Search the web. Returns snippets and source URLs. **Cost: 1 credit (basic) / 2 (advanced).**

Key parameters:
- `query` (required) — search query
- `search_depth` — `basic` | `advanced` | `fast` | `ultra-fast` (default: `basic`)
- `topic` — `general` | `news` | `finance` (default: `general`)
- `max_results` — 5-20 (default: 5)
- `time_range` — `day` | `week` | `month` | `year`
- `include_domains` / `exclude_domains` — filter by site
- `country` — full country name (e.g., "United States"), not ISO codes

### tavily-extract

Extract page content from URLs. **Cost: 1 credit per 5 URLs (basic) / 2 (advanced).**

Key parameters:
- `urls` (required) — list of URLs
- `query` — rerank chunks by relevance to this query
- `extract_depth` — `basic` | `advanced` (use advanced for LinkedIn, protected sites)
- `format` — `markdown` | `text` (default: `markdown`)

### tavily-crawl

Crawl a website from a root URL. **Cost: 1 credit per 5 pages (basic) / 2 (advanced).**

Key parameters:
- `url` (required) — root URL
- `max_depth` — how deep to explore (default: 1)
- `limit` — max pages to process (default: 50)
- `instructions` — natural language page filter (doubles cost)
- `select_paths` — regex path patterns (e.g., `/docs/.*`)
- `extract_depth` — `basic` | `advanced`

### tavily-map

Map a site's URL structure without extracting content. **Cost: 1 credit per 10 pages.**

Key parameters:
- `url` (required) — root URL
- `max_depth` — depth of exploration (default: 1)
- `limit` — max pages (default: 50)
- `instructions` — natural language filter (doubles cost)

### credit-status

Check remaining credits. No parameters, no cost. Returns per-key utilization
and total remaining percentage.

## Credit Conservation Rules

1. Always use `search_depth: "basic"` unless the user specifically needs advanced
2. Keep `max_results` low — 5 is usually enough
3. For crawl/map, start with `limit: 10-20` and increase only if needed
4. Avoid `instructions` on crawl/map unless necessary (doubles cost)
5. Use `tavily-map` + `tavily-extract` instead of `tavily-crawl` when you only
   need specific pages — map is cheaper than crawl
6. Check `credit-status` before large batch operations

## Common Patterns

**Quick web search:**
```
tavily-search(query: "topic", max_results: 5)
```

**Recent news:**
```
tavily-search(query: "topic", topic: "news", time_range: "week")
```

**Extract a specific page:**
```
tavily-extract(urls: ["https://docs.example.com/api"], query: "relevant aspect")
```

**Map then extract (credit-efficient):**
```
# Step 1: Discover pages (cheap)
tavily-map(url: "https://docs.example.com", max_depth: 2, limit: 30)

# Step 2: Extract only the relevant URLs found above
tavily-extract(urls: ["url1", "url2", "url3"])
```

**Domain-scoped search:**
```
tavily-search(query: "error handling", include_domains: ["github.com", "stackoverflow.com"])
```
