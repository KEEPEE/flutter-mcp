# flutter-docs-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI coding agents **live Flutter, Dart SDK and pub.dev documentation** instead of whatever the model happens to remember. Pages are scraped from the official docs at request time, cached locally, and returned as clean markdown — so generated Flutter code targets real, current APIs rather than deprecated or invented ones.

- **Flutter classes** — scraped live from [api.flutter.dev](https://api.flutter.dev) (widgets, material, cupertino, foundation, services, rendering, …)
- **Dart SDK classes** — scraped live from [api.dart.dev](https://api.dart.dev) (dart:core, dart:async, dart:collection, dart:io, …)
- **pub.dev packages** — metadata + README from the pub.dev API
- **Local search index** over ~3,500 Flutter / Dart classes, rebuilt at most every 7 days, with a stale fallback when the network fails
- **SQLite TTL cache** so repeated lookups are instant
- **Politeness layer** — robots.txt (RFC 9309), per-host throttle incl. `Crawl-delay`, `Retry-After`, conditional GET and request budgets ([details](#caching--politeness))

## The five tools

| Tool | What it does |
|---|---|
| `flutter_docs` | Resolve one identifier (`ListView`, `material.AppBar`, `dart:async.Future`, `pub:dio`) to a single documentation page as markdown. |
| `flutter_search` | Ranked name search over the local index of all Flutter + Dart SDK classes, enums, mixins and typedefs. |
| `pub_package` | pub.dev package metadata (version, publisher, likes, pub points) plus its README as markdown. |
| `flutter_status` | Real health check: index size/age, cache stats, live probes of api.flutter.dev and pub.dev, and politeness counters. |
| `health_check` | Server liveness and version. |

Every tool returns a plain dict. Failures come back as `{"error": …, "suggestion": …}` — a bad lookup never raises into the MCP layer, and no tool delegates to another tool.

## Requirements

- Python 3.10 or newer (developed and tested on 3.12)
- [`uv`](https://docs.astral.sh/uv/) for the one-command install below (`uvx` ships with it)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`); this package does not support the mcp 2.x rename of `FastMCP`

## Install & run

### One command — no checkout, no token

```bash
uvx --from git+https://github.com/KEEPEE/flutter-mcp.git flutter-docs
```

This builds the package in an isolated environment and starts the stdio MCP server. It prints nothing on purpose: stdout is the protocol channel. Stop it with `Ctrl-C`.

After the repository is updated, force `uv` to re-resolve the commit:

```bash
uvx --refresh --from git+https://github.com/KEEPEE/flutter-mcp.git flutter-docs
```

### Local checkout — fastest startup, editable while developing

```bash
git clone https://github.com/KEEPEE/flutter-mcp.git
cd flutter-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/flutter-docs          # stdio MCP server
```

## MCP client configuration

### Generic stdio client (Claude Desktop, Cursor, Cline, …)

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

With an explicit cache directory (any `env` you set is passed straight through to the server):

```json
{
  "mcpServers": {
    "flutter-docs": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/flutter-mcp.git",
        "flutter-docs"
      ],
      "env": {
        "FLUTTER_DOCS_MCP_CACHE_DIR": "/tmp/flutter-docs-cache"
      }
    }
  }
}
```

### DeepSeek Harness (`cordis`-style plugin list)

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

To skip the build at client start, point `command` at the console script of a checkout instead — `command: /path/to/flutter-mcp/.venv/bin/flutter-docs` with `args: []`.

## Tools reference

### `flutter_docs(identifier, topic=None, max_tokens=8000)`

Resolves an identifier to one documentation page. Identifier forms, tried in this order:

| Form | Meaning |
|---|---|
| `pub:dio`, `pub:dio:5.11.1` | pub.dev package: description + README |
| `dart:async.Future` | Dart SDK class on api.dart.dev (the library keeps its colon) |
| `material.AppBar`, `widgets.ListView` | Flutter class on api.flutter.dev, library given |
| `ListView` | exact match in the local index → `widgets`/`material` fallback → pub.dev |

`topic` keeps only the section whose heading contains it (`methods`, `properties`, `examples`, `constructors`, …) plus the class title; if nothing matches, the full page is returned with a `note` listing the available headings. `max_tokens` is a rough budget (1 token ≈ 4 characters): the markdown is cut at a line boundary and `truncated` is set.

Returns `{"type", "identifier", "url", "title", "content", "truncated", "cached"}`, plus an optional `note`.

```jsonc
// flutter_docs({ "identifier": "material.AppBar", "topic": "properties", "max_tokens": 300 })
{
  "type": "flutter_class",
  "identifier": "material.AppBar",
  "url": "https://api.flutter.dev/flutter/material/AppBar-class.html",
  "content": "# AppBar class\n\nA Material Design app bar.\n\nAn app bar consists of a toolbar and potentially other widgets … [truncated: showing ~296 of ~4094 estimated tokens]",
  "truncated": true,
  "cached": false,
  "title": "AppBar class"
}
```

```jsonc
// flutter_docs({ "identifier": "dart:async.Future", "topic": "methods", "max_tokens": 250 })
{
  "type": "dart_class",
  "identifier": "dart:async.Future",
  "url": "https://api.dart.dev/dart-async/Future-class.html",
  "content": "# Future<T> class abstract interface\n\nThe result of an asynchronous computation. … ## Methods\n\nasStream() → Stream<T> …",
  "truncated": true,
  "cached": true,
  "title": "Future< T > class abstract interface"
}
```

A name that resolves nowhere reports every place it looked:

```jsonc
// flutter_docs({ "identifier": "NopeXYZ" })
{
  "error": "could not resolve 'NopeXYZ': flutter widgets: not found (HTTP 404): https://api.flutter.dev/flutter/widgets/NopeXYZ-class.html; flutter material: not found (HTTP 404): …; pub.dev: not found (HTTP 404): …",
  "suggestion": "try flutter_search('NopeXYZ') to find the right name"
}
```

### `flutter_search(query, limit=8)`

Ranks the local index by name similarity (exact > prefix > substring > fuzzy). Returns `{"query", "count", "results": [{name, library, kind, url, score}], "index_stale"}`.

```jsonc
// flutter_search({ "query": "text field", "limit": 3 })
{
  "query": "text field",
  "count": 3,
  "results": [
    { "name": "TextField",     "library": "material",   "kind": "class", "url": "https://api.flutter.dev/flutter/material/TextField-class.html",     "score": 0.9474 },
    { "name": "TextFormField", "library": "material",   "kind": "class", "url": "https://api.flutter.dev/flutter/material/TextFormField-class.html", "score": 0.7826 },
    { "name": "BitField",      "library": "foundation", "kind": "class", "url": "https://api.flutter.dev/flutter/foundation/BitField-class.html",    "score": 0.6667 }
  ],
  "index_stale": false
}
```

### `pub_package(package_name, version=None, max_tokens=6000)`

pub.dev metadata plus the README. `package_name` is exact and case-sensitive; `version` pins a release. Returns `{"name", "version", "description", "publisher", "likes", "pub_points", "url", "readme", "truncated", "cached"}`; `publisher` / `likes` / `pub_points` may be `null` when pub.dev does not report them.

```jsonc
// pub_package({ "package_name": "dio", "max_tokens": 150 })
{
  "name": "dio",
  "version": "5.11.1",
  "description": "A powerful HTTP networking package,\nsupports Interceptors,\nAborting and canceling a request,\nCustom adapters, Transformers, etc.\n",
  "publisher": "flutter.cn",
  "likes": "8.36k",
  "pub_points": 160,
  "url": "https://pub.dev/api/packages/dio",
  "readme": "# dio\n\n… [truncated: showing ~137 of ~7837 estimated tokens]",
  "truncated": true,
  "cached": true
}
```

### `flutter_status()`

Probes api.flutter.dev and pub.dev with a light GET (10 s timeout) and reports the index and cache state. `overall` is `ok`, `degraded` or `error`. The `politeness` block is diagnostics only and never changes `overall`.

```jsonc
// flutter_status()
{
  "server": "flutter-docs-mcp",
  "version": "0.2.0",
  "checks": {
    "search_index":    { "status": "ok", "entries": 3503, "built_at": "2026-10-06T08:46:45.893682+00:00", "stale": false },
    "cache":           { "status": "ok", "entries": 5, "expired": 0 },
    "api_flutter_dev": { "status": "ok", "http_status": 200 },
    "pub_dev":         { "status": "ok", "http_status": 200 }
  },
  "overall": "ok",
  "politeness": {
    "status": "ok", "disabled": false,
    "requests": 3, "robots_requests": 2, "robots_rows": 2, "robots_fetches": 2, "robots_cache_hits": 1,
    "blocked_by_robots": 0, "throttle_waits": 3, "throttle_sleep_s": 1.203,
    "host_delays": { "api.flutter.dev": 0.0, "pub.dev": 0.0 },
    "budgets": { "fetch:api.flutter.dev": [2, 6] },
    "budget_denied": 0, "conditional": 0, "revalidated_304": 0,
    "retries_429": 0, "retries_transport": 0, "stalls": 0, "errors": 0
  }
}
```

### `health_check()`

`{"status": "ok", "server": "flutter-docs-mcp", "version": "0.2.0"}` — no network, no cache; safe as a liveness probe.

## Caching & politeness

### Cache

Everything lives in one directory: `~/.cache/flutter-docs-mcp` by default, overridable with `FLUTTER_DOCS_MCP_CACHE_DIR`.

| File | Contents | TTL |
|---|---|---|
| `cache.db` | fetched pages (parsed result + raw body + `ETag` / `Last-Modified`), the search index, pub.dev data | docs 7 days, pub.dev 1 day, index 7 days |
| `robots.db` | the politeness layer's robots.txt cache | 7 days per host |

Delete the files to force a full refresh. A cache problem is never a tool failure: if the directory is unwritable the server simply runs without a cache and says so in `flutter_status().checks.cache`.

### Politeness

Every outbound request — `fetch_*`, the index build and the `flutter_status` probes — goes through one small internal module, [`src/flutter_docs_mcp/politeness.py`](src/flutter_docs_mcp/politeness.py) (stdlib + `httpx`, no extra dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards and would report rules such as `Disallow: /pypi/*/json` as allowed. A disallowed URL returns `{"error": "… blocked by robots.txt …"}` — no request is made and no exception escapes. Files are cached 7 days per host, including negative results (404 / 403 / 5xx), so a host costs one robots request per week.
- **Per-host throttle, including `Crawl-delay`.** Requests to one host are spaced 0.35–0.9 s apart, or by the site's own `Crawl-delay` when it declares one. A cold index build is ~31 requests, which is exactly where that delay is felt — that is the point.
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request is retried once. A response that stalls past the read timeout escalates the delay instead of being retried blindly.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached result, so refreshing an expired entry costs a `304` instead of a full download.
- **Request budgets.** One `fetch_*` call may make at most **6 requests per host** — the robots fetch, every retry and every redirect hop all pay. A cold `pub_package` legitimately costs 3 (robots + API JSON + package page). An index build gets **40 layer calls per host**; it charges one unit per page and the layer does robots and any hop inside it (measured cold build: 24 units on api.flutter.dev, 7 on api.dart.dev).
- **Host allowlist.** Only `api.flutter.dev`, `api.dart.dev` and `pub.dev` can be contacted, so a malformed identifier or a surprising redirect cannot turn a docs lookup into a request to somebody else's site.

The layer never raises and never changes a tool's return shape. `flutter_status` reports its counters under the top-level `politeness` key.

### Opt-out

```bash
export FLUTTER_DOCS_MCP_POLITENESS_DISABLED=1
```

This single switch turns off robots.txt, the throttle, retries and conditional GET at once. **Use it at your own risk:** you take over responsibility for respecting each site's crawling rules, and you are far more likely to be rate-limited or blocked. There is no partial opt-out.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/python -m pytest -q              # 149 offline tests; fixtures in tests/fixtures/
.venv/bin/python scripts/politeness_smoke.py /tmp/flutter-docs-smoke-cache
.venv/bin/python scripts/e2e_mcp_test.py
```

- `pytest` is fully offline: HTTP is simulated with `httpx.MockTransport` and time/jitter are injected, so the suite is deterministic and runs in seconds.
- `scripts/politeness_smoke.py <cache-dir>` is a **live measurement**, not a test: it drives the real tools against the real docs sites and prints what the politeness layer did (requests, robots, throttle, conditional GET, budgets).
- `scripts/e2e_mcp_test.py` spawns the installed `flutter-docs` console script and speaks newline-delimited JSON-RPC to it (`initialize` → `tools/list` → every tool → a negative case → `health_check`). Exit code 0 means every check passed. Set `FLUTTER_DOCS_MCP_E2E_CMD` to run a different server command.

## Troubleshooting

**The first `flutter_docs` call takes half a minute.** That is the cold search-index build: ~31 page fetches across api.flutter.dev and api.dart.dev, spaced by the per-host throttle (a measured cold run spent ~29 s just waiting between requests). It happens once every 7 days; later calls hit the cached index. Delete `cache.db` and you pay for it again.

**`request budget exhausted for scope 'fetch:pub.dev'`.** One `fetch_*` call hit its cap of 6 requests to that host. Redirect chains, `429` retries and transport retries all consume the budget, so this usually means the site redirected more than expected or the connection kept failing. The tool returns an error dict instead of hammering the host; `flutter_status().politeness.budgets` shows how much each scope spent.

**`"partial": true`, or fewer search results than expected.** An index build ran out of its 40-calls-per-host budget and stopped early, returning a partial index with a `partial_reason` such as `request budget of 40 per host exhausted for index:api.flutter.dev; the index is incomplete`. A partial index is **not cached**, so the next run rebuilds it. `flutter_search` still works — it just knows fewer classes — and `flutter_status().checks.search_index.entries` tells you how many it has.

**`"stale": true` / `index_stale: true`.** The rebuild failed (offline, DNS failure, 5xx) and the server fell back to the older cached index instead of failing the lookup.

**Nothing is cached and `overall` is `degraded`.** Read `flutter_status().checks.cache.error` — an unwritable `FLUTTER_DOCS_MCP_CACHE_DIR` (read-only mount, missing permission) is the usual cause. Tools keep working without a cache; they are just slower and noisier on the network.

**A lookup returns `blocked by robots.txt`.** The site's rules disallow that path for this user agent, and the request was not sent. Fetch the page yourself, or accept the consequences of the opt-out switch above.

## License & attribution

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Michal Gaspierik.

The **design** of the politeness layer was inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0): a TTL-cached robots store, the wildcard rule translation, and a per-domain rate limiter with escalating backoff. The implementation is a clean-room rewrite in this project's own synchronous stdlib-plus-`httpx` style — no line was transcribed, translated or mechanically adapted from Crawl4AI, and Crawl4AI is not a dependency of this package. Three defects of the original design are fixed (robots `fetched_at` refresh, negative-result caching, `Crawl-delay` support). The GPL-3.0 part of Crawl4AI — its vendored `html2text` fork — is deliberately excluded: no code, data or dependency from that tree is used or shipped here. The full statement is in [NOTICE](NOTICE).

This project is also a clean-room replacement for [adamsmaka/flutter-mcp](https://github.com/adamsmaka/flutter-mcp), whose main branch pins `mcp` to a 2.x development commit while importing the 1.x-only `FastMCP`, and which recurses between its "unified" tool and the deprecated tool it delegates to. The useful part of that project — fetching real docs — is kept here as fetchers → parsers → cache → search index → tools, with no circular delegation, a pinned 1.x SDK, error-dict failures and an offline test suite.
