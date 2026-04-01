---
name: tavily-router
description: Search the web, extract page content, crawl sites, and map URLs via the Tavily MCP gateway. Routes requests across multiple API keys with credit-aware load balancing. Use when the user needs web search, content extraction, site crawling, or URL discovery.
argument-hint: "[tool-name] [params...]"
---

# Tavily Router (deep-research)

Multi-key Tavily MCP gateway. Drop-in replacement for the official Tavily MCP
that distributes requests across multiple API keys to multiply free-tier
credits (1,000/key/month).

## When to Activate

- User needs current web information, news, or facts
- User asks to search for something ("search for", "look up", "find")
- Extracting content from one or more URLs
- Crawling a website for pages matching criteria
- Mapping a site's URL structure
- Checking remaining Tavily credit budget

## MCP Server

The server is named `tavily` and exposes 5 tools. If already configured,
tools appear as `mcp__tavily__tavily-search`, etc.

**Setup (remote HTTP):**

```bash
claude mcp add tavily -s user -t http http://vps:8087/mcp
```

**Or via `.mcp.json`:**

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

## Tools

### tavily-search

Search the web for current information. Returns snippets and source URLs.

```
tavily-search(query: "latest FastMCP release", search_depth: "basic", max_results: 5)
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `query` | string | required | The search query |
| `search_depth` | `basic` \| `advanced` \| `fast` \| `ultra-fast` | `basic` | `advanced` costs 2 credits instead of 1 |
| `topic` | `general` \| `news` \| `finance` | `general` | Category of search |
| `max_results` | integer (5-20) | 5 | Number of results |
| `time_range` | `day` \| `week` \| `month` \| `year` | none | Filter by recency |
| `start_date` | string | none | Format: `YYYY-MM-DD` |
| `end_date` | string | none | Format: `YYYY-MM-DD` |
| `include_domains` | string[] | none | Only search these domains |
| `exclude_domains` | string[] | none | Skip these domains |
| `country` | string | none | Full country name (e.g., "United States") |
| `include_images` | boolean | false | Include image URLs |
| `include_raw_content` | boolean | false | Include full HTML content |

**Credit cost:** 1 (basic/fast/ultra-fast) or 2 (advanced)

### tavily-extract

Extract page content from URLs as markdown or text.

```
tavily-extract(urls: ["https://docs.example.com/api"], query: "authentication")
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `urls` | string[] | required | URLs to extract |
| `query` | string | none | Rerank extracted chunks by relevance to this query |
| `extract_depth` | `basic` \| `advanced` | `basic` | Use `advanced` for protected sites, LinkedIn, tables |
| `format` | `markdown` \| `text` | `markdown` | Output format |
| `include_images` | boolean | false | Include images |

**Credit cost:** 1 per 5 URLs (basic) or 2 per 5 URLs (advanced)

### tavily-crawl

Crawl a website starting from a URL with configurable depth and breadth.

```
tavily-crawl(url: "https://docs.example.com", max_depth: 2, limit: 20, instructions: "Find API reference pages")
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `url` | string | required | Root URL to begin crawl |
| `max_depth` | integer (>=1) | 1 | How far from root URL to explore |
| `max_breadth` | integer (>=1) | 20 | Links to follow per page |
| `limit` | integer (>=1) | 50 | Total pages to process |
| `instructions` | string | none | Natural language filter for pages (doubles cost) |
| `select_paths` | string[] | none | Regex path patterns (e.g., `/docs/.*`) |
| `select_domains` | string[] | none | Regex domain patterns |
| `extract_depth` | `basic` \| `advanced` | `basic` | `advanced` for tables/embedded content |
| `format` | `markdown` \| `text` | `markdown` | Output format |

**Credit cost:** 1 per 5 pages (basic) or 2 per 5 pages (advanced)

### tavily-map

Map a website's URL structure without extracting content.

```
tavily-map(url: "https://example.com", max_depth: 2, limit: 100)
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `url` | string | required | Root URL to begin mapping |
| `max_depth` | integer (>=1) | 1 | Depth of exploration |
| `max_breadth` | integer (>=1) | 20 | Links per page |
| `limit` | integer (>=1) | 50 | Total pages to process |
| `instructions` | string | none | Natural language filter (doubles cost) |
| `select_paths` | string[] | none | Regex path patterns |
| `select_domains` | string[] | none | Regex domain patterns |

**Credit cost:** 1 per 10 pages (or 2 with instructions)

### credit-status

Check remaining credits across all configured API keys. No parameters, no
credit cost.

```
credit-status()
```

Returns per-key breakdown and totals with utilization percentages:

```json
{
  "keys": [
    { "key": "tvly-dev...xqb", "used": 42, "limit": 1000, "remaining": 958, "utilization_pct": 4.2 },
    { "key": "tvly-dev...8s5", "used": 37, "limit": 1000, "remaining": 963, "utilization_pct": 3.7 }
  ],
  "total_used": 79,
  "total_remaining": 1921,
  "total_limit": 2000,
  "total_utilization_pct": 4.0
}
```

Key IDs are masked for security.

## Best Practices

### Credit conservation

- Use `search_depth: "basic"` unless you specifically need deeper results
- Set `max_results` to the minimum you need (default 5 is usually enough)
- For crawl/map, start with a low `limit` (10-20) and increase if needed
- Avoid `instructions` on crawl/map unless necessary (doubles the cost)
- Use `tavily-map` before `tavily-crawl` to discover URLs cheaply, then
  extract only the ones you need
- Check `credit-status` before large operations

### Choosing the right tool

| Need | Tool | Why |
|------|------|-----|
| Find current information | `tavily-search` | Fast, cheap (1 credit) |
| Read a specific page | `tavily-extract` | Direct content extraction |
| Get content from multiple pages on a site | `tavily-crawl` | Follows links automatically |
| Discover what pages exist on a site | `tavily-map` | URL discovery without content (cheapest) |
| Map first, then extract specific pages | `tavily-map` then `tavily-extract` | Most credit-efficient for targeted extraction |

### Common patterns

**Research a topic:**
```
tavily-search(query: "topic", search_depth: "advanced", max_results: 10)
```

**Extract docs from a known URL:**
```
tavily-extract(urls: ["https://docs.example.com/api"], query: "relevant aspect")
```

**Discover and extract from a docs site:**
```
# Step 1: Map the site structure
tavily-map(url: "https://docs.example.com", max_depth: 2, limit: 50)

# Step 2: Extract only relevant pages found in step 1
tavily-extract(urls: ["url1", "url2", "url3"])
```

**Search within specific domains:**
```
tavily-search(query: "error handling", include_domains: ["github.com", "stackoverflow.com"])
```

**Get recent news:**
```
tavily-search(query: "topic", topic: "news", time_range: "week")
```
