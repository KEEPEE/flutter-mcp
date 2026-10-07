"""flutter-docs-mcp: MCP server exposing Flutter / Dart SDK docs and pub.dev info.

Tools exposed over MCP (stdio transport):

- ``health_check`` — trivial liveness check + server version.
- ``flutter_docs(identifier, topic=None, max_tokens=8000)`` — resolve an
  identifier to a single documentation page as clean markdown:
    * ``pub:<name>[:<version>]``   → pub.dev package (description + README)
    * ``dart:<lib>.<Class>``       → Dart SDK class on api.dart.dev
    * ``<lib>.<Class>``            → Flutter class on api.flutter.dev
    * plain name                   → exact match in the local search index,
      then a widgets/material fallback, then pub.dev as a last resort.
  An optional ``topic`` keeps only the matching section (e.g. "methods");
  ``max_tokens`` truncates the markdown at a line boundary.
- ``flutter_search(query, limit=8)`` — ranked name search over the local index
  of all Flutter + Dart SDK classes / enums / mixins / typedefs.
- ``pub_package(package_name, version=None, max_tokens=6000)`` — pub.dev
  package metadata (version, description, publisher, likes, pub points) plus
  the README as markdown.
- ``flutter_status()`` — real health check: search index state, cache stats,
  and live HTTP probes of api.flutter.dev and pub.dev.

Design rules (hard requirements):

- No tool ever delegates to another tool; every tool calls the fetchers /
  search / cache modules directly.
- Every tool returns a plain dict. On failure: ``{"error": ..., "suggestion": ...}``.
  Exceptions never escape to the MCP layer and nothing recurses.
- Fetched content is cached in :class:`flutter_docs_mcp.cache.DocCache` keyed
  by source URL (docs TTL 7 days, pub README TTL 1 day).
- Every outbound request — fetchers, index build and the status probes — goes
  through :mod:`flutter_docs_mcp.politeness` (robots.txt, per-host throttle,
  ``Retry-After``, conditional GET, request budgets). The layer never raises
  and never changes a tool's return shape; ``flutter_status`` reports its
  counters under the extra top-level key ``politeness``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__, fetchers, search
from .cache import DocCache, default_db_path
from .politeness import default_robots_db_path

__all__ = [
    "mcp",
    "health_check",
    "flutter_docs",
    "flutter_search",
    "pub_package",
    "flutter_status",
    "main",
]

mcp = FastMCP("flutter-docs")

# TTLs (seconds) for cached fetched content, keyed by source URL.
DOCS_TTL_SECONDS = 7 * 24 * 3600    # api.flutter.dev / api.dart.dev pages
PUB_README_TTL_SECONDS = 24 * 3600  # pub.dev package data + README

_FLUTTER_API_BASE = "https://api.flutter.dev/flutter"
_DART_API_BASE = "https://api.dart.dev"
_PUB_API_BASE = "https://pub.dev/api/packages"

# Light GET targets for flutter_status endpoint probes.
_STATUS_PROBES = {
    "api_flutter_dev": f"{_FLUTTER_API_BASE}/widgets/ListView-class.html",
    "pub_dev": f"{_PUB_API_BASE}/dio",
}

_CACHE: DocCache | None = None
#: Set when the cache could not be opened at all (A13 B3 / A12 F-A12-2).  It
#: holds the reason so ``flutter_status`` can say so instead of lying "ok", and
#: it makes the failure sticky: we do not retry a directory that is read-only
#: on every single tool call.
_CACHE_ERROR: str | None = None


# ---------------------------------------------------------------------------
# Cache helpers (key = source URL)
# ---------------------------------------------------------------------------

def _cache() -> DocCache | None:
    """Lazily create the process-wide :class:`DocCache`, or ``None`` if unusable.

    A13 B3 (A12 F-A12-2): an unwritable / read-only cache directory used to
    raise ``OperationalError: attempt to write a readonly database`` out of
    every tool.  The contract is now the same as in java-spring-mcp,
    python-docs-mcp and js-ts-mcp: **a cache problem must never turn into a
    tool failure** — the cache is simply absent and the tool degrades to
    fetching without it.  ``flutter_status`` reports the reason.
    """
    global _CACHE, _CACHE_ERROR
    if _CACHE is not None:
        return _CACHE
    if _CACHE_ERROR is not None:
        return None
    try:
        _CACHE = DocCache()
    except Exception as exc:  # unwritable dir, read-only DB, bad path, …
        _CACHE_ERROR = f"{type(exc).__name__}: {exc}"
        return None
    return _CACHE


def _cache_get(url: str) -> tuple[dict | None, bool]:
    """Return ``(stored_result, hit)`` for a cached fetch result under ``url``."""
    cache = _cache()
    if cache is None:
        return None, False
    try:
        raw = cache.get(url)
    except Exception:
        return None, False
    if raw is None:
        return None, False
    try:
        data = json.loads(raw)
    except ValueError:
        return None, False
    if not isinstance(data, dict) or not data.get("ok"):
        return None, False
    return data, True


def _cache_set(url: str, result: dict, ttl_seconds: int) -> None:
    """Store a successful fetch result under ``url``; never raises.

    ``set_value`` (not ``set``) on purpose: the fetcher keeps the raw body and
    the ``etag`` / ``Last-Modified`` of the same URL in the same row, and
    caching the parsed result must not wipe them — that is what makes the next
    refresh a conditional GET instead of a full download.
    """
    cache = _cache()
    if cache is None:
        return
    try:
        cache.set_value(url, json.dumps(result), ttl_seconds)
    except Exception:
        pass  # a cache failure must never break a successful fetch


# ---------------------------------------------------------------------------
# Source-URL builders + cached fetch wrappers
# ---------------------------------------------------------------------------

def _flutter_doc_url(class_name: str, library: str) -> str:
    return f"{_FLUTTER_API_BASE}/{library}/{class_name}-class.html"


def _dart_doc_url(class_name: str, library: str) -> str:
    return f"{_DART_API_BASE}/{library.replace(':', '-')}/{class_name}-class.html"


def _pub_api_url(package_name: str, version: str | None = None) -> str:
    url = f"{_PUB_API_BASE}/{package_name}"
    if version:
        url = f"{url}/versions/{version}"
    return url


def _fetch_flutter_cached(class_name: str, library: str) -> tuple[dict, bool]:
    """fetchers.fetch_flutter_class_doc with DocCache (TTL 7 days)."""
    url = _flutter_doc_url(class_name, library)
    cached, hit = _cache_get(url)
    if hit:
        return cached, True
    result = fetchers.fetch_flutter_class_doc(class_name, library)
    if result.get("ok"):
        _cache_set(url, result, DOCS_TTL_SECONDS)
    return result, False


def _fetch_dart_cached(class_name: str, library: str) -> tuple[dict, bool]:
    """fetchers.fetch_dart_class_doc with DocCache (TTL 7 days)."""
    url = _dart_doc_url(class_name, library)
    cached, hit = _cache_get(url)
    if hit:
        return cached, True
    result = fetchers.fetch_dart_class_doc(class_name, library)
    if result.get("ok"):
        _cache_set(url, result, DOCS_TTL_SECONDS)
    return result, False


def _fetch_pub_cached(package_name: str, version: str | None = None) -> tuple[dict, bool]:
    """fetchers.fetch_pub_package with DocCache (TTL 1 day)."""
    url = _pub_api_url(package_name, version)
    cached, hit = _cache_get(url)
    if hit:
        return cached, True
    result = fetchers.fetch_pub_package(package_name, version)
    if result.get("ok"):
        _cache_set(url, result, PUB_README_TTL_SECONDS)
    return result, False


# ---------------------------------------------------------------------------
# Markdown post-processing (topic filter + token-budget truncation)
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _split_sections(markdown: str) -> tuple[list[str], list[dict]]:
    """Split markdown into ``(preamble_lines, sections)``.

    ``sections`` is a list of ``{"level", "text", "lines"}`` in document order;
    lines inside fenced code blocks are never treated as headings.
    """
    preamble: list[str] = []
    sections: list[dict] = []
    current: dict | None = None
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        match = None if in_fence else _HEADING_RE.match(line)
        if match:
            if current is not None:
                sections.append(current)
            current = {
                "level": len(match.group(1)),
                "text": match.group(2).strip(),
                "lines": [line],
            }
        elif current is None:
            preamble.append(line)
        else:
            current["lines"].append(line)
    if current is not None:
        sections.append(current)
    return preamble, sections


def _apply_topic(markdown: str, topic: str) -> tuple[str, list[str] | None]:
    """Keep only the section(s) whose heading contains ``topic`` (case-insensitive).

    The class title/description (preamble + level-1 headings) is always kept,
    as are subsections nested under a matched heading. When nothing matches,
    returns the markdown unchanged plus the list of available headings so the
    caller can surface them in a note.
    """
    preamble, sections = _split_sections(markdown)
    needle = topic.lower()

    keep: set[int] = set()
    matched_any = False
    active_level: int | None = None
    for i, section in enumerate(sections):
        if needle in section["text"].lower():
            keep.add(i)
            active_level = section["level"]
            matched_any = True
        elif section["level"] == 1:
            keep.add(i)  # class title / top-level description
        elif active_level is not None and section["level"] > active_level:
            keep.add(i)  # subsection of a matched section
        else:
            active_level = None

    if not matched_any:
        return markdown, [s["text"] for s in sections]

    parts: list[str] = []
    preamble_text = "\n".join(preamble).strip()
    if preamble_text:
        parts.append(preamble_text)
    for i, section in enumerate(sections):
        if i in keep:
            part = "\n".join(section["lines"]).strip()
            if part:
                parts.append(part)
    return "\n\n".join(parts), None


def _truncate_markdown(text: str, max_tokens: int) -> tuple[str, bool]:
    """Truncate ``text`` to roughly ``max_tokens`` tokens (1 token ≈ 4 chars).

    Cuts at a line boundary and appends a trailing estimate note. A
    non-positive or non-int ``max_tokens`` disables truncation. Returns
    ``(markdown, truncated)``.
    """
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        return text, False
    total_tokens = len(text) // 4
    char_budget = max_tokens * 4
    if len(text) <= char_budget:
        return text, False
    cut = text[:char_budget]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    shown_tokens = len(cut) // 4
    note = f"\n\n[truncated: showing ~{shown_tokens} of ~{total_tokens} estimated tokens]"
    return cut + note, True


def _finalize_markdown(markdown: str, topic: str | None) -> tuple[str, str | None]:
    """Apply the optional topic filter; returns ``(markdown, note_or_None)``."""
    if not topic:
        return markdown, None
    filtered, headings = _apply_topic(markdown, str(topic))
    if headings is None:
        return filtered, None
    return filtered, (
        f"no section matching topic '{topic}' on this page; "
        f"available sections: {', '.join(headings)}"
    )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def _doc_result(doc_type: str, identifier: str, result: dict, cached: bool,
                topic: str | None, max_tokens: int) -> dict:
    """Success shape for flutter_docs from an ok dartdoc fetcher result."""
    markdown = result.get("markdown") or ""
    content, note = _finalize_markdown(markdown, topic)
    truncated = False
    if max_tokens is not None:
        content, truncated = _truncate_markdown(content, max_tokens)
    out: dict = {
        "type": doc_type,
        "identifier": identifier,
        "url": result.get("url"),
        "content": content,
        "truncated": truncated,
        "cached": cached,
    }
    if result.get("title"):
        out["title"] = result["title"]
    if note:
        out["note"] = note
    return out


def _pub_doc_result(identifier: str, result: dict, cached: bool,
                    topic: str | None, max_tokens: int) -> dict:
    """Success shape for the pub.dev path of flutter_docs."""
    name = result.get("name") or identifier
    version = result.get("version")
    title = f"{name} {version}".strip() if version else name
    parts = []
    if result.get("description"):
        parts.append(str(result["description"]).strip())
    if result.get("readme_markdown"):
        parts.append(str(result["readme_markdown"]).strip())
    markdown = "\n\n".join(parts)
    content, note = _finalize_markdown(markdown, topic)
    truncated = False
    if max_tokens is not None:
        content, truncated = _truncate_markdown(content, max_tokens)
    out: dict = {
        "type": "pub_package",
        "identifier": identifier,
        "url": result.get("url"),
        "title": title,
        "content": content,
        "truncated": truncated,
        "cached": cached,
    }
    if note:
        out["note"] = note
    return out


def _find_exact_entry(name: str, entries: list[dict]) -> dict | None:
    """Exact name match in index entries — case-sensitive first, then case-insensitive."""
    for entry in entries:
        if entry.get("name") == name:
            return entry
    lowered = name.lower()
    for entry in entries:
        if str(entry.get("name") or "").lower() == lowered:
            return entry
    return None


def _fetch_index_entry(name: str, entry: dict) -> tuple[dict, bool, str]:
    """Fetch the doc for a search-index entry; returns ``(result, cached, doc_type)``.

    The site + library are derived from the entry URL:
    ``api.flutter.dev/flutter/{lib}/{file}`` → fetch_flutter_class_doc with that
    lib; ``api.dart.dev/{lib}/...`` → fetch_dart_class_doc.
    """
    url = entry.get("url") or ""
    if url.startswith(_FLUTTER_API_BASE + "/"):
        rest = url[len(_FLUTTER_API_BASE) + 1:]
        library = rest.split("/", 1)[0] or "widgets"
        result, cached = _fetch_flutter_cached(name, library)
        return result, cached, "flutter_class"
    if url.startswith(_DART_API_BASE + "/"):
        rest = url[len(_DART_API_BASE) + 1:]
        library = rest.split("/", 1)[0] or "dart-core"
        # Dart index URLs use the dash form (dart-async); the fetcher accepts it.
        result, cached = _fetch_dart_cached(name, library)
        return result, cached, "dart_class"
    # Unknown URL shape: fall back to the entry's own library field.
    library = entry.get("library") or "widgets"
    if "flutter.dev" in url:
        result, cached = _fetch_flutter_cached(name, library)
        return result, cached, "flutter_class"
    result, cached = _fetch_dart_cached(name, library)
    return result, cached, "dart_class"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def health_check() -> dict:
    """Check that the server is up and report its version."""
    return {"status": "ok", "server": "flutter-docs-mcp", "version": __version__}


@mcp.tool()
def flutter_docs(identifier: str, topic: str | None = None, max_tokens: int = 8000) -> dict:
    """Fetch one Flutter / Dart SDK / pub.dev documentation page as clean markdown.

    Identifier forms (resolved in this order):
      - "pub:dio" or "pub:dio:5.4.0"        → pub.dev package (description + README)
      - "dart:async.Future"                 → Dart SDK class (api.dart.dev)
      - "material.AppBar" / "widgets.ListView" → Flutter class with explicit library
      - "ListView"                          → exact match in the local search index,
        then a widgets/material fallback, then pub.dev as a last resort

    Args:
        identifier: one of the forms above.
        topic: optional section filter (e.g. "methods", "properties", "examples",
            "constructors") — keeps only the matching section plus the class
            title/description; if no section matches, the full content is returned
            with a "note" listing the available section headings.
        max_tokens: rough token budget for the returned markdown (1 token ≈ 4
            chars); longer output is cut at a line boundary with a trailing note.

    Returns {"type", "identifier", "url", "title", "content", "truncated",
    "cached"} (plus an optional "note"), or {"error", "suggestion"} on failure.
    """
    try:
        ident = str(identifier or "").strip()
        if not ident:
            return {
                "error": "empty identifier",
                "suggestion": "pass a class name like 'ListView', 'material.AppBar', 'dart:async.Future' or 'pub:dio'",
            }

        # (a) pub:<name>[:<version>]
        if ident.startswith("pub:"):
            rest = ident[4:]
            pieces = rest.split(":")
            package_name = pieces[0].strip()
            version = pieces[1].strip() if len(pieces) > 1 and pieces[1].strip() else None
            if not package_name:
                return {
                    "error": f"empty package name in identifier '{ident}'",
                    "suggestion": "use the form 'pub:dio' or 'pub:dio:5.4.0'",
                }
            result, cached = _fetch_pub_cached(package_name, version)
            if not result.get("ok"):
                return {
                    "error": f"could not fetch pub package '{package_name}': {result.get('error')}",
                    "suggestion": "check the exact (case-sensitive) package name on pub.dev",
                }
            return _pub_doc_result(ident, result, cached, topic, max_tokens)

        # (b) dart:<lib>.<Class> — the library itself carries the colon
        # ("dart:async.Future" → library "dart:async", class "Future").
        if ident.startswith("dart:"):
            if "." not in ident:
                return {
                    "error": f"invalid Dart identifier '{ident}'",
                    "suggestion": "use the form 'dart:async.Future' (library.ClassName)",
                }
            library, class_name = (part.strip() for part in ident.split(".", 1))
            if not library or not class_name:
                return {
                    "error": f"invalid Dart identifier '{ident}'",
                    "suggestion": "use the form 'dart:async.Future' (library.ClassName)",
                }
            result, cached = _fetch_dart_cached(class_name, library)
            if not result.get("ok"):
                return {
                    "error": f"could not fetch Dart class doc: {result.get('error')}",
                    "suggestion": f"check the library and class name, or try flutter_search('{class_name}')",
                }
            return _doc_result("dart_class", ident, result, cached, topic, max_tokens)

        # (c) <lib>.<Class> — single dot, explicit Flutter library
        if ident.count(".") == 1:
            library, class_name = (part.strip() for part in ident.split(".", 1))
            if not library or not class_name:
                return {
                    "error": f"invalid identifier '{ident}'",
                    "suggestion": "use the form 'material.AppBar' (library.ClassName)",
                }
            result, cached = _fetch_flutter_cached(class_name, library)
            if not result.get("ok"):
                return {
                    "error": f"could not fetch Flutter class doc: {result.get('error')}",
                    "suggestion": f"the class may live in another library — try flutter_search('{class_name}')",
                }
            return _doc_result("flutter_class", ident, result, cached, topic, max_tokens)

        # (d) plain name: search index → widgets/material → pub.dev
        errors: list[str] = []
        index = search.load_index()
        entries = index.get("entries") if isinstance(index, dict) else None
        entry = _find_exact_entry(ident, entries or [])
        if entry is not None:
            result, cached, doc_type = _fetch_index_entry(ident, entry)
            if result.get("ok"):
                return _doc_result(doc_type, ident, result, cached, topic, max_tokens)
            errors.append(f"index entry {entry.get('url')}: {result.get('error')}")

        for library in ("widgets", "material"):
            result, cached = _fetch_flutter_cached(ident, library)
            if result.get("ok"):
                return _doc_result("flutter_class", ident, result, cached, topic, max_tokens)
            errors.append(f"flutter {library}: {result.get('error')}")

        result, cached = _fetch_pub_cached(ident, None)
        if result.get("ok"):
            return _pub_doc_result(ident, result, cached, topic, max_tokens)
        errors.append(f"pub.dev: {result.get('error')}")

        return {
            "error": f"could not resolve '{ident}': " + "; ".join(errors),
            "suggestion": f"try flutter_search('{ident}') to find the right name",
        }
    except Exception as exc:  # defensive: never let an exception reach the MCP layer
        return {
            "error": f"unexpected error in flutter_docs: {exc.__class__.__name__}: {exc}",
            "suggestion": "retry, or use flutter_search() to locate the class first",
        }


@mcp.tool()
def flutter_search(query: str, limit: int = 8) -> dict:
    """Search Flutter and Dart SDK class names (plus enums/mixins/typedefs).

    Ranks the local search index by name similarity (exact > prefix > substring
    > fuzzy). Use this to find the right identifier before calling flutter_docs.

    Args:
        query: text to match against class names, e.g. "text field" or "ListView".
        limit: maximum number of results (default 8).

    Returns {"query", "count", "results": [{name, library, kind, url, score}],
    "index_stale"} or {"error", "suggestion"} when the index is unavailable.
    """
    try:
        q = str(query or "").strip()
        if not q:
            return {
                "error": "empty query",
                "suggestion": "pass some text, e.g. flutter_search('text field')",
            }
        index = search.load_index()
        entries = index.get("entries") if isinstance(index, dict) else None
        if not isinstance(entries, list) or (not entries and index.get("error")):
            detail = index.get("error", "unexpected index shape") if isinstance(index, dict) else "unexpected index shape"
            return {
                "error": f"search index unavailable: {detail}",
                "suggestion": "try again later — the index rebuilds itself when api.flutter.dev / api.dart.dev are reachable",
            }
        results = search.search(q, limit=limit, index=index)
        return {
            "query": q,
            "count": len(results),
            "results": results,
            "index_stale": bool(index.get("stale", False)),
        }
    except Exception as exc:  # defensive: never let an exception reach the MCP layer
        return {
            "error": f"unexpected error in flutter_search: {exc.__class__.__name__}: {exc}",
            "suggestion": "try again later",
        }


@mcp.tool()
def pub_package(package_name: str, version: str | None = None, max_tokens: int = 6000) -> dict:
    """Fetch pub.dev package metadata plus its README as markdown.

    Args:
        package_name: exact pub.dev package name (case-sensitive), e.g. "dio".
        version: optional specific version, e.g. "5.4.0" (default: latest).
        max_tokens: rough token budget for the returned README (1 token ≈ 4
            chars); longer output is cut at a line boundary with a trailing note.

    Returns {"name", "version", "description", "publisher", "likes", "pub_points",
    "url", "readme", "truncated", "cached"} or {"error", "suggestion"} on failure.
    publisher / likes / pub_points may be null when pub.dev does not report them.
    """
    try:
        name = str(package_name or "").strip()
        if not name:
            return {
                "error": "empty package name",
                "suggestion": "pass the exact pub.dev package name, e.g. 'dio'",
            }
        version = str(version).strip() if version else None
        result, cached = _fetch_pub_cached(name, version)
        if not result.get("ok"):
            return {
                "error": f"could not fetch pub package '{name}': {result.get('error')}",
                "suggestion": "check the exact (case-sensitive) name on pub.dev",
            }
        readme, truncated = _truncate_markdown(result.get("readme_markdown") or "", max_tokens)
        return {
            "name": result.get("name") or name,
            "version": result.get("version"),
            "description": result.get("description"),
            "publisher": result.get("publisher"),
            "likes": result.get("likes"),
            "pub_points": result.get("pub_points"),
            "url": result.get("url"),
            "readme": readme,
            "truncated": truncated,
            "cached": cached,
        }
    except Exception as exc:  # defensive: never let an exception reach the MCP layer
        return {
            "error": f"unexpected error in pub_package: {exc.__class__.__name__}: {exc}",
            "suggestion": "retry with the exact package name from pub.dev",
        }


@mcp.tool()
def flutter_status() -> dict:
    """Health check: search index state, cache stats, and live API probes.

    Probes api.flutter.dev and pub.dev with a light GET (10s timeout) and reports
    per-check status plus an overall verdict ("ok" / "degraded" / "error").
    Never raises — individual failures only mark that check as error.

    Also returns a top-level "politeness" block with the politeness layer's
    counters: robots cache rows/fetches/hits, requests blocked by robots.txt,
    throttle waits and per-host delays, conditional GETs / 304 revalidations,
    429 and transport retries, request budgets and whether the layer is
    disabled (FLUTTER_DOCS_POLITENESS_DISABLED). It is diagnostics only and
    never affects "overall".
    """
    checks: dict[str, dict] = {}

    # -- search index --------------------------------------------------------
    try:
        index = search.load_index()
        entries = index.get("entries") if isinstance(index, dict) else None
        if isinstance(entries, list) and (entries or not index.get("error")):
            status = "ok"
        else:
            status = "error"
        checks["search_index"] = {
            "status": status,
            "entries": len(entries) if isinstance(entries, list) else 0,
            "built_at": index.get("built_at") if isinstance(index, dict) else None,
            "stale": bool(index.get("stale", False)) if isinstance(index, dict) else False,
        }
    except Exception:
        checks["search_index"] = {"status": "error", "entries": 0, "built_at": None, "stale": False}

    # -- cache -----------------------------------------------------------------
    # A13 B3: an unusable cache directory must be *announced*, not silent.  The
    # tools keep working without a cache; ``overall`` goes to "degraded" so a
    # user with a read-only cache dir learns why nothing is cached.
    cache = _cache()
    if cache is None:
        checks["cache"] = {
            "status": "error",
            "entries": 0,
            "expired": 0,
            "error": _CACHE_ERROR or "cache unavailable",
            "cache_db": default_db_path(),
        }
    else:
        try:
            stats = cache.stats()
            checks["cache"] = {
                "status": "ok",
                "entries": int(stats.get("entries", 0)),
                "expired": int(stats.get("expired", 0)),
            }
        except Exception as exc:
            checks["cache"] = {
                "status": "error",
                "entries": 0,
                "expired": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

    # -- endpoint probes ---------------------------------------------------------
    for key, url in _STATUS_PROBES.items():
        checks[key] = _probe_endpoint(url)

    statuses = [c.get("status") for c in checks.values()]
    if all(s == "ok" for s in statuses):
        overall = "ok"
    elif any(s == "ok" for s in statuses):
        overall = "degraded"
    else:
        overall = "error"

    return {
        "server": "flutter-docs-mcp",
        "version": __version__,
        "checks": checks,
        "overall": overall,
        "politeness": _politeness_report(),
    }


def _probe_endpoint(url: str) -> dict:
    """Light GET of ``url`` (10s timeout); returns status + http_status. Never raises.

    Goes through the politeness layer like every other request — a health check
    that ignores robots.txt or hammers the site would defeat the point of it.
    """
    try:
        with httpx.Client(
            timeout=10.0, follow_redirects=True, headers={"User-Agent": fetchers.USER_AGENT}
        ) as client:
            response = fetchers.get_politeness().get(client, url)
        if response.blocked_by_robots or response.error:
            return {"status": "error", "http_status": response.status_code}
        status_code = int(response.status_code) if response.status_code is not None else 0
        return {"status": "ok" if status_code < 400 else "error", "http_status": status_code}
    except Exception:
        return {"status": "error", "http_status": None}


def _robots_cache_rows() -> int | None:
    """How many robots.txt records the politeness SQLite cache holds (read-only).

    ``None`` when the cache file does not exist or cannot be read — the layer
    then runs on its in-memory fallback.
    """
    try:
        path = default_robots_db_path()
        if not os.path.exists(path):
            return 0
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM robots").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return None


def _politeness_report() -> dict:
    """Politeness counters for ``flutter_status``; never raises.

    Deliberately *outside* ``checks``: it is diagnostics, not a health signal,
    so it must not drag ``overall`` to "degraded" on its own.
    """
    try:
        stats = fetchers.get_politeness().stats()
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
    return {
        "status": "ok",
        "disabled": bool(stats.get("disabled", False)),
        "requests": int(stats.get("requests", 0)),
        # A8 F1/F3: robots attempts are counted apart from content requests;
        # ``requests + robots_requests`` is the true wire total.
        "robots_requests": int(stats.get("robots_requests", 0)),
        "robots_rows": _robots_cache_rows(),
        "robots_fetches": int(stats.get("robots_fetches", 0)),
        "robots_cache_hits": int(stats.get("robots_cache_hits", 0)),
        "robots_negative": int(stats.get("robots_negative", 0)),
        "blocked_by_robots": int(stats.get("blocked_by_robots", 0)),
        "throttle_waits": int(stats.get("throttle_waits", 0)),
        "throttle_sleep_s": round(float(stats.get("throttle_sleep_s", 0.0)), 3),
        # A8 F1: the robots subset of those waits — the evidence that a
        # robots.txt fetch waits in the same per-host queue as a page request.
        "robots_throttle_waits": int(stats.get("robots_throttle_waits", 0)),
        "robots_throttle_sleep_s": round(float(stats.get("robots_throttle_sleep_s", 0.0)), 3),
        # A8 F2/F4: hops the layer walked itself, and challenge-hook hits.
        "redirect_hops": int(stats.get("redirect_hops", 0)),
        "challenge_detected": int(stats.get("challenge_detected", 0)),
        "challenge_retries": int(stats.get("challenge_retries", 0)),
        "host_delays": stats.get("hosts", {}),
        "budgets": stats.get("budgets", {}),
        "budget_denied": int(stats.get("budget_denied", 0)),
        "conditional": int(stats.get("conditional", 0)),
        "revalidated_304": int(stats.get("revalidated_304", 0)),
        "retries_429": int(stats.get("retries_429", 0)),
        "retries_transport": int(stats.get("retries_transport", 0)),
        "stalls": int(stats.get("stalls", 0)),
        "errors": int(stats.get("errors", 0)),
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
