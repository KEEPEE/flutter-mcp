# flutter-docs-mcp

Real-time Flutter / Dart documentation and pub.dev package info as an MCP (Model Context Protocol) server. Built so AI agents generate Flutter code against **real, current docs** instead of hallucinated or deprecated APIs.

- **Flutter classes** — scraped live from [api.flutter.dev](https://api.flutter.dev) (all libraries: widgets, material, cupertino, foundation, services, rendering, …)
- **Dart SDK classes** — scraped live from [api.dart.dev](https://api.dart.dev) (dart:core, dart:async, dart-collection, dart:io, dart:math, …)
- **pub.dev packages** — metadata + README from the pub.dev API
- **Local search index** over ~3500 Flutter/Dart classes (rebuilt at most every 7 days, stale-fallback on network failure)
- **SQLite TTL cache** so repeated lookups are instant and don't hammer the doc sites
- **Politeness layer** — robots.txt (RFC 9309), per-host throttle incl. `Crawl-delay`, `Retry-After`, conditional GET, request budgets ([details](#politeness))

## Tools

| Tool | What it does |
|---|---|
| `flutter_docs` | Unified lookup. Identifier forms: `ListView`, `material.AppBar`, `dart:async.Future`, `pub:dio` (or `pub:dio:5.11.1`). Optional `topic` filter (`methods`, `properties`, `examples`, …) and `max_tokens` truncation. |
| `flutter_search` | Fuzzy search over the local class index. Returns ranked results with absolute doc URLs — call `flutter_docs` with the chosen name. |
| `pub_package` | pub.dev package metadata (version, publisher, likes, pub points) + README markdown. Optional pinned `version`. |
| `flutter_status` | Real health check: search index size/age/staleness, cache stats, live probes of api.flutter.dev and pub.dev, plus politeness counters (robots cache, throttle, blocks, budgets). |
| `health_check` | Server liveness + version. |

All tools return plain dicts; failures come back as `{"error": ..., "suggestion": ...}` — the server never crashes on a bad lookup.

## Requirements

- Python 3.10+ (tested on 3.12)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`) — this package does not support the mcp 2.x rename of FastMCP.

## Run it

### Option A — `uvx` straight from this repo (no manual install)

```bash
# TOKEN = your GitLab personal access token for github.com/KEEPEE
uvx --from "git+https://github.com/KEEPEE/flutter-mcp.git" flutter-docs
```

Add `--refresh` to force re-pulling the latest commit after an update:

```bash
uvx --refresh --from "git+https://github.com/KEEPEE/flutter-mcp.git" flutter-docs
```

### Option B — local venv (fastest startup, no token in config)

```bash
git clone https://github.com/KEEPEE/flutter-mcp.git
cd flutter-mcp
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e .
.venv/bin/flutter-docs        # starts the stdio MCP server
```

## MCP client configuration

### DSH (this machine) — in `~/.dsh-home/profiles/web/cordis.patch.yml`

```yaml
- id: mcp-flutter-docs
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: flutter-docs
    transport: stdio
    command: uvx
    args:
      [
        '--from',
        'git+https://github.com/KEEPEE/flutter-mcp.git',
        'flutter-docs'
      ]
```

(or point `command` at a pre-installed venv binary, e.g. `/home/keepee/.dsh-home/tools/flutter-docs-mcp/venv/bin/flutter-docs`, to avoid the token in config entirely).

### Claude Desktop / any generic MCP client

```json
{
  "mcpServers": {
    "flutter-docs": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/flutter-mcp.git",
        "flutter-docs"
      ]
    }
  }
}
```

## Cache location

`~/.cache/flutter-docs-mcp/cache.db` (override with `FLUTTER_DOCS_MCP_CACHE_DIR`). Docs are cached for 7 days, pub.dev READMEs for 1 day. The same directory also holds `robots.db` (the politeness layer's robots.txt cache, 7 days per host). Delete the files to force a full refresh.

## Politeness

Every outbound request — `fetch_*`, the search-index build and the `flutter_status` probes — goes through one small internal layer, `src/flutter_docs_mcp/politeness.py` (stdlib + `httpx`, no new dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards, so it reports PyPI-style rules such as `Disallow: /pypi/*/json` as allowed. A disallowed URL returns `{"ok": false, "error": "… blocked by robots.txt …"}` — no request is made and no exception escapes. Files are cached 7 days per host in `robots.db`, including negative results (404/403/5xx), so a host costs one robots request per week.
- **Per-host throttle, including `Crawl-delay`.** Sequential requests to one host are spaced (0.35–0.9 s by default, or the site's own `Crawl-delay` when it declares one). A cold index build is ~33 requests, so this is where the delay is felt — that is the point: it is what keeps a full rebuild from looking like an attack.
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request retried once. A response that stalls past the read timeout also escalates the delay instead of being retried blindly.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached result, so refreshing an expired entry costs a `304` instead of re-downloading the page.
- **Request budgets.** One `fetch_*` call may make at most 6 requests per host — since the A8 port a budget unit *is* one request on the wire, so the robots.txt fetch, every `429`/transport retry and every redirect hop all pay (a cold `pub_package` legitimately costs 3: robots + API JSON + package page). An index build gets 40 layer calls per host, which is not a wire cap: the build charges one unit per page and the layer does robots.txt and any hop inside it (measured cold build: 24 units on `api.flutter.dev`, 7 on `api.dart.dev`). A build that hits its cap stops early and returns a **partial** index (`"partial": true`) instead of raising, and it is not cached.
- **Host allowlist.** Only `api.flutter.dev`, `api.dart.dev` and `pub.dev` can be contacted, so a malformed identifier or an unexpected redirect cannot turn a docs lookup into a request somewhere else.

The layer never raises and never changes a tool's return shape; `flutter_status` reports its counters under the top-level `politeness` key (robots cache rows, throttle waits and per-host delays, blocks, retries, budgets).

**Opt-out** (at your own risk — you become responsible for whatever the site's rules say):

```bash
export FLUTTER_DOCS_MCP_POLITENESS_DISABLED=1
```

That turns off robots, throttle, retry and conditional GET in one switch. There is no partial opt-out.

**Attribution:** the layer's design is inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0) — the robots store, the wildcard translation and the per-domain rate limiter. It is a clean-room rewrite, not a copy, and Crawl4AI is not a dependency here. See [`NOTICE`](NOTICE).

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # offline unit tests (fixtures in tests/fixtures/)
.venv/bin/python scripts/e2e_mcp_test.py      # live end-to-end: spawns the server, exercises every tool
```

## Design notes (why this exists)

This is a clean-room replacement for <https://github.com/adamsmaka/flutter-mcp>, which on its main branch has: an unresolvable dependency declaration (pins `mcp` to a 2.x dev git commit while importing the 1.x-only `FastMCP`), an open bug where internal calls pass `max_tokens=` to a parameter named `tokens`, and — underneath that — infinite mutual recursion between its "unified" tool and the deprecated tool it delegates to. The core value (fetching real docs) is a thin scraper, so this project keeps exactly that: fetchers → parsers → cache → search index → tools, with no circular delegation, pinned 1.x SDK, error-dict failures, and an offline test suite backed by real page fixtures.
