# flutter-docs-mcp

Real-time Flutter / Dart documentation and pub.dev package info as an MCP (Model Context Protocol) server. Built so AI agents generate Flutter code against **real, current docs** instead of hallucinated or deprecated APIs.

- **Flutter classes** — scraped live from [api.flutter.dev](https://api.flutter.dev) (all libraries: widgets, material, cupertino, foundation, services, rendering, …)
- **Dart SDK classes** — scraped live from [api.dart.dev](https://api.dart.dev) (dart:core, dart:async, dart-collection, dart:io, dart:math, …)
- **pub.dev packages** — metadata + README from the pub.dev API
- **Local search index** over ~3500 Flutter/Dart classes (rebuilt at most every 7 days, stale-fallback on network failure)
- **SQLite TTL cache** so repeated lookups are instant and don't hammer the doc sites

## Tools

| Tool | What it does |
|---|---|
| `flutter_docs` | Unified lookup. Identifier forms: `ListView`, `material.AppBar`, `dart:async.Future`, `pub:dio` (or `pub:dio:5.11.1`). Optional `topic` filter (`methods`, `properties`, `examples`, …) and `max_tokens` truncation. |
| `flutter_search` | Fuzzy search over the local class index. Returns ranked results with absolute doc URLs — call `flutter_docs` with the chosen name. |
| `pub_package` | pub.dev package metadata (version, publisher, likes, pub points) + README markdown. Optional pinned `version`. |
| `flutter_status` | Real health check: search index size/age/staleness, cache stats, live probes of api.flutter.dev and pub.dev. |
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

`~/.cache/flutter-docs-mcp/cache.db` (override with `FLUTTER_DOCS_MCP_CACHE_DIR`). Docs are cached for 7 days, pub.dev READMEs for 1 day. Delete the file to force a full refresh.

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # offline unit tests (fixtures in tests/fixtures/)
.venv/bin/python scripts/e2e_mcp_test.py      # live end-to-end: spawns the server, exercises every tool
```

## Design notes (why this exists)

This is a clean-room replacement for <https://github.com/adamsmaka/flutter-mcp>, which on its main branch has: an unresolvable dependency declaration (pins `mcp` to a 2.x dev git commit while importing the 1.x-only `FastMCP`), an open bug where internal calls pass `max_tokens=` to a parameter named `tokens`, and — underneath that — infinite mutual recursion between its "unified" tool and the deprecated tool it delegates to. The core value (fetching real docs) is a thin scraper, so this project keeps exactly that: fetchers → parsers → cache → search index → tools, with no circular delegation, pinned 1.x SDK, error-dict failures, and an offline test suite backed by real page fixtures.
