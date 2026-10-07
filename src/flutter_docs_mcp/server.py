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
- ``flutter_mentions(text, max_tokens=4000)`` — find every
  ``@flutter_mcp <identifier>`` mention in a text and resolve each one to its
  documentation, version constraints included (``provider:^6.0.0``,
  ``dio:>=5.0.0 <6.0.0``, ``provider:6.1.5``, ``provider:latest``). Exactly one
  result per mention, in document order.
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

from . import __version__, fetchers, mentions, search, versions
from .cache import DocCache, default_db_path
from .politeness import default_robots_db_path

__all__ = [
    "mcp",
    "health_check",
    "flutter_docs",
    "flutter_search",
    "pub_package",
    "flutter_mentions",
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


#: Cache key suffix for the published-release list of a package. The ``kv`` row
#: is keyed by source URL and already holds the parsed package (plus its
#: validators), so the version list needs its own key rather than a second read
#: of ``.../api/packages/{name}``.
_PUB_VERSIONS_SUFFIX = "#versions"

#: A published release list changes far less often than a README is re-read.
PUB_VERSIONS_TTL_SECONDS = 6 * 3600


def _fetch_pub_versions_cached(package_name: str) -> tuple[dict, bool]:
    """fetchers.fetch_pub_versions with DocCache (TTL 6 hours)."""
    key = _pub_api_url(package_name) + _PUB_VERSIONS_SUFFIX
    cached, hit = _cache_get(key)
    if hit:
        return cached, True
    result = fetchers.fetch_pub_versions(package_name)
    if result.get("ok"):
        _cache_set(key, result, PUB_VERSIONS_TTL_SECONDS)
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


def _apply_topic_report(markdown: str, topic: str) -> tuple[str, list[str] | None, list[str]]:
    """Keep only the section(s) whose heading contains ``topic`` — and report the cut.

    Returns ``(markdown, all_headings_if_nothing_matched, dropped_section_names)``.

    The class title/description (preamble + level-1 headings) is always kept,
    as are subsections nested under a matched heading. When nothing matches,
    returns the markdown unchanged plus the list of available headings so the
    caller can surface them in a note — and ``dropped`` is empty, because
    nothing really was dropped.

    ``dropped`` is the reason ``truncated`` can be truthful: a topic filter cuts
    the page too (whole member sections disappear), and a caller must be able to
    see *which* sections are missing instead of inferring it from a short body.
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
        return markdown, [s["text"] for s in sections], []

    parts: list[str] = []
    dropped: list[str] = []
    preamble_text = "\n".join(preamble).strip()
    if preamble_text:
        parts.append(preamble_text)
    for i, section in enumerate(sections):
        if i not in keep:
            dropped.append(section["text"])
            continue
        part = "\n".join(section["lines"]).strip()
        if part:
            parts.append(part)
    return "\n\n".join(parts), None, dropped


def _apply_topic(markdown: str, topic: str) -> tuple[str, list[str] | None]:
    """Backwards-compatible two-value form of :func:`_apply_topic_report`."""
    filtered, headings, _dropped = _apply_topic_report(markdown, topic)
    return filtered, headings


#: Rough conversion used by **both** the budget decision and every reported
#: token number, so a response can never quote a different rule than the one
#: that produced its cut.
CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (1 token ≈ 4 characters)."""
    return len(text) // 4


def _fit_to_budget(text: str, max_tokens: int, source_tokens: int | None = None) -> tuple[str, dict]:
    """Cut ``text`` to roughly ``max_tokens`` tokens and report exactly what that did.

    Returns ``(markdown, detail)`` with::

        detail = {"budget_cut": bool, "tokens": int, "source_tokens": int,
                  "max_tokens": int | None}

    - ``tokens`` is the estimate of the **body actually returned** (the trailing
      marker excluded); ``source_tokens`` is the estimate of the page it came
      from. ``~X of ~Y`` therefore always describes the real payload.
    - The cut is at a line boundary, so the body alone always fits the budget.
      When the budget can also hold the marker, the marker is paid for out of
      the same budget (second pass) so the whole payload fits; when it cannot
      (a budget of a few dozen characters) the body is kept and the marker's
      own cost is disclosed as ``tokens_payload`` in the ``truncation`` block
      rather than being hidden.
    - ``budget_cut`` is False when nothing was cut, including when
      ``max_tokens`` is missing or non-positive (truncation disabled).
    """
    own_tokens = _estimate_tokens(text)
    source_tokens = own_tokens if source_tokens is None else int(source_tokens)

    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        return text, {
            "budget_cut": False,
            "tokens": own_tokens,
            "source_tokens": source_tokens,
            "max_tokens": None,
        }

    char_budget = max_tokens * CHARS_PER_TOKEN
    if len(text) <= char_budget:
        return text, {
            "budget_cut": False,
            "tokens": own_tokens,
            "source_tokens": source_tokens,
            "max_tokens": max_tokens,
        }

    def cut_at_line(limit: int) -> str:
        cut = text[:limit]
        newline = cut.rfind("\n")
        if newline > 0:
            cut = cut[:newline]
        return cut

    def marker(shown: int) -> str:
        return f"\n\n[truncated: showing ~{shown} of ~{source_tokens} estimated tokens]"

    body = cut_at_line(char_budget)
    shown = _estimate_tokens(body)
    note = marker(shown)
    if len(body) + len(note) > char_budget:
        # Pay for the marker out of the same budget — but never at the cost of
        # an empty body: a budget too small for both keeps the body and reports
        # the marker's cost instead.
        room = char_budget - len(note)
        if room >= 8:
            body = cut_at_line(room)
            shown = _estimate_tokens(body)
            note = marker(shown)

    return body + note, {
        "budget_cut": True,
        "tokens": shown,
        "source_tokens": source_tokens,
        "max_tokens": max_tokens,
    }


def _truncate_markdown(text: str, max_tokens: int) -> tuple[str, bool]:
    """Backwards-compatible two-value form of :func:`_fit_to_budget`."""
    content, detail = _fit_to_budget(text, max_tokens)
    return content, detail["budget_cut"]


def _finalize_markdown(markdown: str, topic: str | None) -> tuple[str, str | None, list[str]]:
    """Apply the optional topic filter; returns ``(markdown, note_or_None, dropped)``."""
    if not topic:
        return markdown, None, []
    filtered, headings, dropped = _apply_topic_report(markdown, str(topic))
    if headings is None:
        return filtered, None, dropped
    return filtered, (
        f"no section matching topic '{topic}' on this page; "
        f"available sections: {', '.join(headings)}"
    ), dropped


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def _truncation_detail(det: dict, dropped: list[str], topic: str | None, content: str) -> dict | None:
    """The ``truncation`` block: what was cut, by which mechanism, how much.

    ``None`` when nothing was cut. ``reasons`` names the mechanism(s) so
    ``truncated: true`` is never ambiguous between "the token budget cut the
    body" and "the topic filter removed whole sections".
    """
    reasons: list[str] = []
    if dropped:
        reasons.append("topic_filter")
    if det["budget_cut"]:
        reasons.append("token_budget")
    if not reasons:
        return None

    explanation: list[str] = []
    if dropped:
        explanation.append(
            f"topic '{topic}' dropped {len(dropped)} section(s): {', '.join(dropped)}"
        )
    if det["budget_cut"]:
        explanation.append(
            f"token budget {det['max_tokens']}: body cut at a line boundary, "
            f"~{det['tokens']} of ~{det['source_tokens']} estimated tokens returned"
        )
    return {
        "reasons": reasons,
        "budget_cut": bool(det["budget_cut"]),
        "topic_filtered": bool(dropped),
        "sections_dropped": list(dropped),
        "tokens_returned": det["tokens"],
        "tokens_source": det["source_tokens"],
        "tokens_payload": _estimate_tokens(content),
        "max_tokens": det["max_tokens"],
        "explanation": "; ".join(explanation),
    }


def _doc_result(doc_type: str, identifier: str, result: dict, cached: bool,
                topic: str | None, max_tokens: int) -> dict:
    """Success shape for flutter_docs from an ok dartdoc fetcher result."""
    markdown = result.get("markdown") or ""
    source_tokens = _estimate_tokens(markdown)
    content, note, dropped = _finalize_markdown(markdown, topic)
    content, det = _fit_to_budget(content, max_tokens, source_tokens)
    out: dict = {
        "type": doc_type,
        "identifier": identifier,
        "url": result.get("url"),
        "content": content,
        # Truthful by construction: True whenever the returned content is not
        # the whole page — a token-budget cut *or* a topic filter.
        "truncated": bool(det["budget_cut"]) or bool(dropped),
        "tokens": det["tokens"],
        "source_tokens": det["source_tokens"],
        "max_tokens": det["max_tokens"],
        "cached": cached,
    }
    if result.get("title"):
        out["title"] = result["title"]
    detail = _truncation_detail(det, dropped, topic, content)
    if detail:
        out["truncation"] = detail
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
    source_tokens = _estimate_tokens(markdown)
    content, note, dropped = _finalize_markdown(markdown, topic)
    content, det = _fit_to_budget(content, max_tokens, source_tokens)
    out: dict = {
        "type": "pub_package",
        "identifier": identifier,
        "url": result.get("url"),
        "title": title,
        "content": content,
        "truncated": bool(det["budget_cut"]) or bool(dropped),
        "tokens": det["tokens"],
        "source_tokens": det["source_tokens"],
        "max_tokens": det["max_tokens"],
        "cached": cached,
    }
    detail = _truncation_detail(det, dropped, topic, content)
    if detail:
        out["truncation"] = detail
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


def _resolve_identifier(ident: str) -> tuple[str | None, dict | None, bool, dict | None]:
    """Resolve an identifier to ``(doc_type, ok_result, cached, error_payload)``.

    ``doc_type`` is ``"pub_package"`` | ``"dart_class"`` | ``"flutter_class"``;
    exactly one of ``ok_result`` / ``error_payload`` is non-``None``. This is the
    single resolution path shared by :func:`flutter_docs` and
    :func:`flutter_mentions` — both tools resolve identically, and neither one
    calls the other (a tool never delegates to another tool).
    """
    # (a) pub:<name>[:<version>]
    if ident.startswith("pub:"):
        rest = ident[4:]
        pieces = rest.split(":")
        package_name = pieces[0].strip()
        version = pieces[1].strip() if len(pieces) > 1 and pieces[1].strip() else None
        if not package_name:
            return None, None, False, {
                "error": f"empty package name in identifier '{ident}'",
                "suggestion": "use the form 'pub:dio' or 'pub:dio:5.4.0'",
            }
        result, cached = _fetch_pub_cached(package_name, version)
        if not result.get("ok"):
            return None, None, False, {
                "error": f"could not fetch pub package '{package_name}': {result.get('error')}",
                "suggestion": "check the exact (case-sensitive) package name on pub.dev",
            }
        return "pub_package", result, cached, None

    # (b) dart:<lib>.<Class> — the library itself carries the colon
    # ("dart:async.Future" → library "dart:async", class "Future").
    if ident.startswith("dart:"):
        if "." not in ident:
            return None, None, False, {
                "error": f"invalid Dart identifier '{ident}'",
                "suggestion": "use the form 'dart:async.Future' (library.ClassName)",
            }
        library, class_name = (part.strip() for part in ident.split(".", 1))
        if not library or not class_name:
            return None, None, False, {
                "error": f"invalid Dart identifier '{ident}'",
                "suggestion": "use the form 'dart:async.Future' (library.ClassName)",
            }
        result, cached = _fetch_dart_cached(class_name, library)
        if not result.get("ok"):
            return None, None, False, {
                "error": f"could not fetch Dart class doc: {result.get('error')}",
                "suggestion": f"check the library and class name, or try flutter_search('{class_name}')",
            }
        return "dart_class", result, cached, None

    # (c) <lib>.<Class> — single dot, explicit Flutter library
    if ident.count(".") == 1:
        library, class_name = (part.strip() for part in ident.split(".", 1))
        if not library or not class_name:
            return None, None, False, {
                "error": f"invalid identifier '{ident}'",
                "suggestion": "use the form 'material.AppBar' (library.ClassName)",
            }
        result, cached = _fetch_flutter_cached(class_name, library)
        if not result.get("ok"):
            return None, None, False, {
                "error": f"could not fetch Flutter class doc: {result.get('error')}",
                "suggestion": f"the class may live in another library — try flutter_search('{class_name}')",
            }
        return "flutter_class", result, cached, None

    # (d) plain name: search index → widgets/material → pub.dev
    errors: list[str] = []
    index = search.load_index()
    entries = index.get("entries") if isinstance(index, dict) else None
    entry = _find_exact_entry(ident, entries or [])
    if entry is not None:
        result, cached, doc_type = _fetch_index_entry(ident, entry)
        if result.get("ok"):
            return doc_type, result, cached, None
        errors.append(f"index entry {entry.get('url')}: {result.get('error')}")

    for library in ("widgets", "material"):
        result, cached = _fetch_flutter_cached(ident, library)
        if result.get("ok"):
            return "flutter_class", result, cached, None
        errors.append(f"flutter {library}: {result.get('error')}")

    result, cached = _fetch_pub_cached(ident, None)
    if result.get("ok"):
        return "pub_package", result, cached, None
    errors.append(f"pub.dev: {result.get('error')}")

    return None, None, False, {
        "error": f"could not resolve '{ident}': " + "; ".join(errors),
        "suggestion": f"try flutter_search('{ident}') to find the right name",
    }


# ---------------------------------------------------------------------------
# Version-constrained pub.dev resolution (used by flutter_mentions)
# ---------------------------------------------------------------------------

def _nearest_versions(wanted: str, available: list[str], limit: int = 5) -> list[str]:
    """Releases of ``available`` closest to ``wanted`` (below first, then above)."""
    ordered = sorted(available, key=versions.sort_key, reverse=True)
    key = versions.sort_key(wanted)
    at_or_below = [v for v in ordered if versions.sort_key(v) <= key][:limit]
    above = [v for v in reversed(ordered) if versions.sort_key(v) > key][:limit]
    return at_or_below + above


def _resolve_pub_version(package_name: str, constraint: str) -> dict:
    """Resolve a version constraint against the releases pub.dev actually publishes.

    Returns ``{"ok": True, "version", "latest", "how"}`` — ``how`` says which
    rule produced it (``"exact"``, ``"newest_matching"`` or ``"latest"``) — or
    ``{"ok": False, "kind": "not_found" | "error", "error", "suggestion",
    "latest"}``. A version is only ever returned when it is a published release
    string taken from pub.dev's own list: an exact pin that pub.dev does not
    have is reported as missing (with the nearest published releases), never
    quietly answered with a different version.
    """
    data, _cached = _fetch_pub_versions_cached(package_name)
    if not data.get("ok"):
        detail = data.get("error") or "unknown error"
        kind = "not_found" if "404" in str(detail) else "error"
        return {
            "ok": False,
            "kind": kind,
            "error": f"could not read the published releases of '{package_name}': {detail}",
            "suggestion": f"check the exact (case-sensitive) package name on pub.dev, or try pub_package('{package_name}')",
        }

    available = [v for v in (data.get("versions") or []) if isinstance(v, str)]
    latest = data.get("latest") or (available[-1] if available else None)

    parsed = versions.parse_constraint(constraint)
    if parsed is None:
        return {
            "ok": False,
            "kind": "error",
            "error": f"unsupported version constraint '{constraint}'",
            "suggestion": "use 'latest', an exact release like '6.1.5', or a range like '^6.0.0' / '>=5.0.0 <6.0.0'",
            "latest": latest,
        }

    if parsed["any"]:
        return {"ok": True, "version": latest, "latest": latest, "how": "latest"}

    if parsed["exact"]:
        wanted = parsed["terms"][0]["version"]
        if wanted in available:
            return {"ok": True, "version": wanted, "latest": latest, "how": "exact"}
        near = _nearest_versions(wanted, available)
        return {
            "ok": False,
            "kind": "not_found",
            "error": f"version '{wanted}' is not a published release of '{package_name}'",
            "suggestion": (
                f"published releases nearest to '{wanted}': {', '.join(near)}"
                if near else f"'{package_name}' has no published releases on pub.dev"
            ),
            "latest": latest,
            "nearest_versions": near,
        }

    resolved = versions.newest_matching(available, parsed)
    if resolved is None:
        return {
            "ok": False,
            "kind": "not_found",
            "error": f"no published release of '{package_name}' satisfies the constraint '{constraint}'",
            "suggestion": f"published releases include: {', '.join(available[-8:])}" if available else f"'{package_name}' has no published releases on pub.dev",
            "latest": latest,
        }
    return {"ok": True, "version": resolved, "latest": latest, "how": "newest_matching"}


# ---------------------------------------------------------------------------
# Mention resolution — one mention, one result, no shared state
# ---------------------------------------------------------------------------

#: Fields copied from a built doc payload into a mention result.
_MENTION_PAYLOAD_FIELDS = (
    "url", "title", "content", "truncated", "tokens", "source_tokens",
    "max_tokens", "cached", "truncation",
)


def _mention_payload(base: dict, doc_type: str, payload: dict,
                     version: str | None, version_note: str | None = None) -> dict:
    """Merge a doc payload into a mention result built from ``base``.

    ``base`` comes from this mention alone, so nothing from another mention can
    leak in: the only inputs are ``base``, ``payload`` and ``version``.
    """
    out = dict(base)
    out["type"] = doc_type
    out["version"] = version
    for key in _MENTION_PAYLOAD_FIELDS:
        if key in payload:
            out[key] = payload[key]
    notes = [note for note in (version_note, payload.get("note")) if note]
    if notes:
        out["note"] = " | ".join(notes)
    return out


def _resolve_pub_mention(base: dict, package_name: str, constraint: str | None,
                         max_tokens: int) -> dict:
    """Resolve one pub.dev mention (optionally version-constrained)."""
    version: str | None = None
    version_note: str | None = None

    if constraint is not None:
        resolution = _resolve_pub_version(package_name, constraint)
        if not resolution.get("ok"):
            out = dict(base)
            out["type"] = "not_found" if resolution.get("kind") == "not_found" else "error"
            out["version"] = None
            out["error"] = resolution["error"]
            out["suggestion"] = resolution["suggestion"]
            if resolution.get("latest"):
                out["latest_on_pub_dev"] = resolution["latest"]
            if resolution.get("nearest_versions"):
                out["nearest_versions"] = resolution["nearest_versions"]
            return out

        version = resolution["version"]
        latest = resolution.get("latest")
        if resolution["how"] == "exact":
            version_note = f"exact release {version} requested and published on pub.dev"
        elif resolution["how"] == "newest_matching":
            version_note = (
                f"constraint '{constraint}' resolved to {version}: the newest published "
                f"release satisfying it"
                + ("" if version == latest else f" (pub.dev's latest is {latest})")
            )
        else:
            version_note = f"no version requested: pub.dev's latest release {version}"

    result, cached = _fetch_pub_cached(package_name, version)
    if not result.get("ok"):
        out = dict(base)
        out["type"] = "error"
        out["version"] = version
        out["error"] = (
            f"could not fetch pub package '{package_name}'"
            + (f" version '{version}'" if version else "")
            + f": {result.get('error')}"
        )
        out["suggestion"] = "check the exact (case-sensitive) package name on pub.dev"
        return out

    identifier = f"pub:{package_name}" + (f":{version}" if version else "")
    payload = _pub_doc_result(identifier, result, cached, None, max_tokens)
    if version_note is None and result.get("version"):
        version_note = f"no version requested: pub.dev's latest release {result['version']}"
    return _mention_payload(base, "pub_package", payload, result.get("version") or version, version_note)


def _resolve_mention(record: dict, max_tokens: int) -> dict:
    """Resolve one parsed mention into a freshly built result dict.

    Every value comes from ``record`` plus this call's own fetches. The function
    reads no module state and no earlier mention's result, and it returns
    exactly one dict — which is what makes a phantom or duplicated entry
    structurally impossible rather than merely unlikely.
    """
    base: dict = {
        "mention": record["mention"],
        "identifier": record["identifier"],
        "requested_constraint": record["constraint"],
    }
    identifier = record["identifier"]
    constraint = record["constraint"]

    # A version tail always means a pub.dev package: Dart/Flutter SDK classes
    # have no releases of their own to pin. "latest" counts as a version tail
    # even though it leaves no constraint behind.
    if record.get("version_requested") or record.get("explicit_pub"):
        return _resolve_pub_mention(base, identifier, constraint, max_tokens)

    # Malformed class identifiers are reported as "error", not "not_found".
    if identifier.startswith("dart:") and "." not in identifier:
        out = dict(base)
        out["type"] = "error"
        out["version"] = None
        out["error"] = f"invalid Dart identifier '{identifier}'"
        out["suggestion"] = "use the form 'dart:async.Future' (library.ClassName)"
        return out
    if "." in identifier and not all(part.strip() for part in identifier.split(".")):
        out = dict(base)
        out["type"] = "error"
        out["version"] = None
        out["error"] = f"invalid identifier '{identifier}'"
        out["suggestion"] = "use the form 'material.AppBar' (library.ClassName) or 'dart:async.Future'"
        return out

    doc_type, result, cached, error = _resolve_identifier(identifier)
    if error is not None:
        out = dict(base)
        out["type"] = "not_found"
        out["version"] = None
        out["error"] = error["error"]
        out["suggestion"] = error["suggestion"]
        return out

    payload = (
        _pub_doc_result(identifier, result, cached, None, max_tokens)
        if doc_type == "pub_package"
        else _doc_result(doc_type, identifier, result, cached, None, max_tokens)
    )
    # A plain name that only matched pub.dev still gets its real version reported.
    resolved_version = result.get("version") if doc_type == "pub_package" else None
    return _mention_payload(base, doc_type, payload, resolved_version)


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
    "tokens", "source_tokens", "max_tokens", "cached"} — plus "truncation" (what
    was cut and why) whenever "truncated" is true, and an optional "note" — or
    {"error", "suggestion"} on failure.
    """
    try:
        ident = str(identifier or "").strip()
        if not ident:
            return {
                "error": "empty identifier",
                "suggestion": "pass a class name like 'ListView', 'material.AppBar', 'dart:async.Future' or 'pub:dio'",
            }

        doc_type, result, cached, error = _resolve_identifier(ident)
        if error is not None:
            return error
        if doc_type == "pub_package":
            return _pub_doc_result(ident, result, cached, topic, max_tokens)
        return _doc_result(doc_type, ident, result, cached, topic, max_tokens)
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
    "url", "readme", "truncated", "tokens", "source_tokens", "max_tokens",
    "cached"} or {"error", "suggestion"} on failure. ``truncated`` is true when
    the README was cut, in which case a ``truncation`` block explains the cut
    and ``tokens`` / ``source_tokens`` give the returned / full-page estimates.
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
        readme_source = result.get("readme_markdown") or ""
        source_tokens = _estimate_tokens(readme_source)
        readme, det = _fit_to_budget(readme_source, max_tokens, source_tokens)
        out: dict = {
            "name": result.get("name") or name,
            "version": result.get("version"),
            "description": result.get("description"),
            "publisher": result.get("publisher"),
            "likes": result.get("likes"),
            "pub_points": result.get("pub_points"),
            "url": result.get("url"),
            "readme": readme,
            "truncated": bool(det["budget_cut"]),
            "tokens": det["tokens"],
            "source_tokens": det["source_tokens"],
            "max_tokens": det["max_tokens"],
            "cached": cached,
        }
        detail = _truncation_detail(det, [], None, readme)
        if detail:
            out["truncation"] = detail
        return out
    except Exception as exc:  # defensive: never let an exception reach the MCP layer
        return {
            "error": f"unexpected error in pub_package: {exc.__class__.__name__}: {exc}",
            "suggestion": "retry with the exact package name from pub.dev",
        }


@mcp.tool()
def flutter_mentions(text: str, max_tokens: int = 4000) -> dict:
    """Parse every @flutter_mcp mention in a text and resolve each one to its docs.

    Mention grammar (identifier forms are the same flutter_docs accepts):
      - "@flutter_mcp provider"              pub.dev package, latest release
      - "@flutter_mcp provider:^6.0.0"       caret constraint
      - "@flutter_mcp provider:6.1.5"        exact published release
      - "@flutter_mcp dio:>=5.0.0 <6.0.0"    range, space separated
      - "@flutter_mcp provider:latest"       explicit latest
      - "@flutter_mcp pub:dio" / "pub:dio:5.4.0"
      - "@flutter_mcp material.AppBar"       Flutter class, library given
      - "@flutter_mcp dart:async.Future"     Dart SDK class
      - "@flutter_mcp Container"             plain name

    A version constraint is answered from pub.dev's own list of published
    releases: an exact pin is returned when published, a range resolves to the
    newest release that satisfies it (pre-releases are excluded unless the
    constraint asks for one), and the constraint that was requested is always
    reported next to the version that was resolved. A version that does not
    exist is reported as missing with the nearest published releases — never
    replaced by a different version.

    Args:
        text: any text containing zero or more @flutter_mcp mentions.
        max_tokens: rough token budget for each mention's documentation payload
            (1 token ≈ 4 chars). Non-positive disables truncation.

    Returns {"mentions": n, "max_tokens": ..., "results": [...]} with exactly
    one entry per mention, in document order. Each entry carries "mention" (as
    written), "type" (flutter_class | dart_class | pub_package | not_found |
    error), the resolved "identifier" and "version", and the docs payload
    ("content", "truncated", "tokens", "source_tokens", "max_tokens", plus
    "truncation" when something was cut). Failures are per-mention
    {"type": "not_found" | "error", "error", "suggestion"} — one bad mention
    never removes or duplicates another mention's entry.
    """
    try:
        if not isinstance(text, str):
            return {
                "error": "text must be a string",
                "suggestion": "pass the text that contains @flutter_mcp mentions",
            }
        records = mentions.parse_mentions(text)
        if not records:
            return {
                "mentions": 0,
                "max_tokens": max_tokens,
                "results": [],
                "note": "no @flutter_mcp mention found in the text",
            }
        results = [_resolve_mention(record, max_tokens) for record in records]
        return {
            "mentions": len(results),
            "max_tokens": max_tokens,
            "results": results,
        }
    except Exception as exc:  # defensive: never let an exception reach the MCP layer
        return {
            "error": f"unexpected error in flutter_mentions: {exc.__class__.__name__}: {exc}",
            "suggestion": "retry with a simpler mention list, e.g. '@flutter_mcp provider:^6.0.0'",
        }


@mcp.tool()
def flutter_status() -> dict:
    """Health check: search index state, cache stats, and live API probes.

    The ``cache`` check gains ``read_only: true`` plus ``read_only_reason``
    when the database cannot be written (P5).  It stays an "ok" check on
    purpose: a cache that only reads is a degraded optimisation, not a sick
    server, so ``overall`` does not change.

    Probes api.flutter.dev and pub.dev with a light GET (10s timeout) and reports
    per-check status plus an overall verdict ("ok" / "degraded" / "error").
    Never raises — individual failures only mark that check as error.

    Also returns a top-level "politeness" block with the politeness layer's
    counters: robots cache rows/fetches/hits, requests blocked by robots.txt,
    throttle waits and per-host delays, conditional GETs / 304 revalidations,
    429 and transport retries, request budgets and whether the layer is
    disabled (FLUTTER_DOCS_MCP_POLITENESS_DISABLED). It is diagnostics only and
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
    # user with an unusable cache dir learns why nothing is cached.  A cache
    # that exists but cannot be **written** is the other case (P5): it stays
    # usable for reads, reports ``read_only`` and leaves ``overall`` alone.
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
            if cache.read_only:
                # P5: a read-only cache is a degraded optimisation, not a
                # broken server — announced, but the check keeps
                # ``status: "ok"`` so ``overall`` is unchanged.
                checks["cache"]["read_only"] = True
                checks["cache"]["read_only_reason"] = cache.read_only_reason
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
