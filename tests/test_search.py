"""Offline tests for flutter_docs_mcp.search (ranking + cached index build).

The network layer is the single module-level function ``search._http_get_text``;
tests monkeypatch it with canned dartdoc-style HTML so nothing touches the
network.
"""

from __future__ import annotations

import json

import pytest

import flutter_docs_mcp.search as search_mod
from flutter_docs_mcp.cache import DocCache
from flutter_docs_mcp.search import build_index, load_index, search


# ---------------------------------------------------------------------------
# Canned HTML fixtures (minimal dartdoc library-overview pages)
# ---------------------------------------------------------------------------

def _lib_page(*sections: tuple[str, list[tuple[str, str]]]) -> str:
    """Build a minimal dartdoc library-overview page.

    ``sections`` is a list of ``(heading, [(name, href), ...])`` pairs.
    """
    parts = ['<html><head><title>lib</title></head><body>', '<div id="dartdoc-main-content">']
    for heading, members in sections:
        parts.append(f"<h2>{heading}</h2>\n<dl>")
        for name, href in members:
            parts.append(
                f'<dt id="{name}"><span class="name"><a href="{href}">{name}</a></span>'
                f"</dt><dd>Some description.</dd>"
            )
        parts.append("</dl>")
    parts.append("</div></body></html>")
    return "".join(parts)


FLUTTER_ROOT_PAGE = (
    "<html><body><nav>"
    '<a href="widgets/">widgets</a>'
    '<a href="material/">material</a>'
    '<a href="dart-core/">dart-core (should be filtered out)</a>'
    '<a href="package-x_x/">package lib (filtered out)</a>'
    '<a href="Android/">platform dir (filtered out)</a>'
    "</nav></body></html>"
)

DART_ROOT_PAGE = (
    "<html><body>"
    '<a href="dart-core/">dart:core</a>'
    '<a href="dart-async/">dart:async</a>'
    '<a href="not-a-lib/">ignored</a>'
    "</body></html>"
)

FLUTTER_WIDGETS_PAGE = _lib_page(
    ("Classes", [
        ("ListView", "widgets/ListView-class.html"),
        # Re-exported class: canonical lib is material, not the page's lib.
        ("TextField", "material/TextField-class.html"),
    ]),
    # Current dartdoc uses PLAIN .html URLs for enums and typedefs (verified
    # on the live site); the kind must come from the section heading.
    ("Enums", [("MainAxisSize", "widgets/MainAxisSize.html")]),
    ("Mixins", [("RestorationMixin", "widgets/RestorationMixin-mixin.html")]),
    ("Typedefs", [("ValueGetter", "foundation/ValueGetter.html")]),
)

FLUTTER_MATERIAL_PAGE = _lib_page(
    ("Classes", [
        # Nested-view href on the material page; must normalize to canonical.
        ("ElevatedButton", "material/ElevatedButton-class.html"),
        ("ListView", "widgets/ListView-class.html"),
    ]),
)

DART_CORE_PAGE = _lib_page(
    ("Classes", [
        ("String", "String-class.html"),
        ("DateTime", "DateTime-class.html"),
    ]),
    # Extension-type page in its own (unrecognized) section: plain .html with
    # no kind suffix → must be skipped.
    ("Extension Types", [("DateTimeCopyWith", "../dart-core/DateTimeCopyWith.html")]),
    # Typedefs: plain URL (kind from section) + suffixed URL (kind from suffix).
    ("Typedefs", [
        ("FutureOr", "../dart-async/FutureOr.html"),
        ("Deprecated", "Deprecated-typedef.html"),
    ]),
    # Curated dartdoc section (unrecognized heading): kind must fall back to
    # the link suffix.
    ("Numbers and booleans", [("int", "int-class.html")]),
)

DART_ASYNC_PAGE = _lib_page(
    ("Classes", [("Future", "Future-class.html"), ("Stream", "Stream-class.html")]),
)

CANNED_PAGES = {
    "https://api.flutter.dev/index.html": FLUTTER_ROOT_PAGE,
    "https://api.flutter.dev/flutter/widgets/": FLUTTER_WIDGETS_PAGE,
    "https://api.flutter.dev/flutter/material/": FLUTTER_MATERIAL_PAGE,
    "https://api.dart.dev/index.html": DART_ROOT_PAGE,
    "https://api.dart.dev/dart-core/": DART_CORE_PAGE,
    "https://api.dart.dev/dart-async/": DART_ASYNC_PAGE,
}


def _fake_http_get_text(url: str):
    return CANNED_PAGES.get(url)


@pytest.fixture
def canned_network(monkeypatch):
    calls = []

    def fake(url: str) -> str | None:
        calls.append(url)
        return CANNED_PAGES.get(url)

    monkeypatch.setattr(search_mod, "_http_get_text", fake)
    return calls


@pytest.fixture
def dead_network(monkeypatch):
    """Network that fails for every URL."""
    monkeypatch.setattr(search_mod, "_http_get_text", lambda url: None)


@pytest.fixture
def cache_dir_env(monkeypatch, tmp_path):
    """Point DocCache's default path at a temp dir (for load_index tests)."""
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


# ---------------------------------------------------------------------------
# search() ranking — hand-built fake index, no network at all
# ---------------------------------------------------------------------------

def _entry(name: str, library: str = "widgets") -> dict:
    return {
        "name": name,
        "library": library,
        "kind": "class",
        "url": f"https://api.flutter.dev/flutter/{library}/{name}-class.html",
    }


def _fake_index() -> dict:
    return {
        "built_at": "2026-01-01T00:00:00+00:00",
        "entries": [
            _entry("TextField", "material"),
            _entry("TextFormField", "material"),
            _entry("Text"),
            _entry("ListView"),
            _entry("ListWheelScrollView"),
            _entry("ListItem"),
            _entry("WidgetItem"),
            _entry("ItmWdgt"),
            _entry("Container"),
        ],
    }


def test_exact_match_ranks_first():
    results = search("ListView", index=_fake_index())
    assert results[0]["name"] == "ListView"
    # Exact match must carry the highest score (tier weight 3 + ratio).
    assert results[0]["score"] >= 3.0
    assert all(results[0]["score"] > r["score"] for r in results[1:])


def test_startswith_beats_substring_and_fuzzy():
    # Query "text": Text=exact, TextField/TextFormField=starts-with, the rest
    # are at best fuzzy. Starts-with entries must all outrank non-starters.
    results = search("text", index=_fake_index())
    names = [r["name"] for r in results]
    assert names[0] == "Text"
    starts = [n for n in names if n.startswith("Text")]
    rest = [n for n in names if not n.startswith("Text")]
    assert set(starts) >= {"TextField", "TextFormField"}
    # Every starts-with result ranks above every non-starts-with result.
    assert max(names.index(n) for n in starts) < min(
        (names.index(n) for n in rest), default=len(names)
    )


def test_substring_beats_fuzzy():
    # Query "item": ListItem=starts-with, WidgetItem=substring, ItmWdgt=fuzzy.
    # ItmWdgt has a *higher raw ratio* than WidgetItem (0.67 vs 0.5) but must
    # still rank below the substring match.
    results = search("item", index=_fake_index())
    names = [r["name"] for r in results]
    assert names.index("ListItem") < names.index("WidgetItem") < names.index("ItmWdgt")


def test_limit_respected():
    results = search("t", limit=2, index=_fake_index())
    assert len(results) == 2
    assert all("score" in r for r in results)
    # Scores are non-increasing.
    scores = [r["score"] for r in search("t", index=_fake_index())]
    assert scores == sorted(scores, reverse=True)


def test_empty_or_none_query_returns_empty_list():
    assert search("", index=_fake_index()) == []
    assert search(None, index=_fake_index()) == []
    assert search("   ", index=_fake_index()) == []


def test_search_augments_entries_with_score_and_keeps_fields():
    results = search("ListView", limit=1, index=_fake_index())
    assert set(results[0]) >= {"name", "library", "kind", "url", "score"}
    assert results[0]["url"].startswith("https://api.flutter.dev/flutter/")


# ---------------------------------------------------------------------------
# build_index / load_index — offline via monkeypatched network layer
# ---------------------------------------------------------------------------

def test_build_index_offline(canned_network):
    index = build_index()
    assert index["built_at"]
    entries = index["entries"]
    assert len(entries) >= 8

    for e in entries:
        assert set(e) == {"name", "library", "kind", "url"}
        assert e["name"] and e["library"] and e["kind"] in {"class", "enum", "mixin", "typedef"}
        assert e["url"].startswith(("https://api.flutter.dev/flutter/", "https://api.dart.dev/"))

    by_url = {e["url"]: e for e in entries}
    # Canonical (normalized) absolute URLs, including cross-library hrefs.
    assert by_url["https://api.flutter.dev/flutter/widgets/ListView-class.html"]["name"] == "ListView"
    assert by_url["https://api.flutter.dev/flutter/material/TextField-class.html"]["kind"] == "class"
    # Enums/typedefs: plain .html URLs, kind from the section heading.
    assert by_url["https://api.flutter.dev/flutter/widgets/MainAxisSize.html"]["kind"] == "enum"
    assert by_url["https://api.flutter.dev/flutter/foundation/ValueGetter.html"]["kind"] == "typedef"
    assert by_url["https://api.flutter.dev/flutter/widgets/RestorationMixin-mixin.html"]["kind"] == "mixin"
    assert by_url["https://api.dart.dev/dart-core/String-class.html"]["library"] == "dart-core"
    assert by_url["https://api.dart.dev/dart-async/FutureOr.html"]["kind"] == "typedef"
    # Unrecognized (curated) section: kind falls back to the link suffix.
    assert by_url["https://api.dart.dev/dart-core/int-class.html"]["kind"] == "class"
    assert by_url["https://api.dart.dev/dart-core/Deprecated-typedef.html"]["kind"] == "typedef"
    # Extension-type pages are not class/enum/mixin/typedef entries.
    assert not any("DateTimeCopyWith" in u for u in by_url)
    # ListView appears once despite being listed on two library pages.
    assert sum(1 for e in entries if e["name"] == "ListView") == 1


def test_load_index_builds_then_serves_from_cache(canned_network, cache_dir_env):
    first = load_index()
    assert first["entries"] and "stale" not in first
    calls_after_first = len(canned_network)
    assert calls_after_first > 0

    second = load_index()  # fresh cached copy → no network
    assert second == first
    assert len(canned_network) == calls_after_first


def test_load_index_force_refresh_rebuilds(canned_network, cache_dir_env):
    load_index()
    calls_after_first = len(canned_network)
    rebuilt = load_index(force_refresh=True)
    assert rebuilt["entries"] and "stale" not in rebuilt
    assert len(canned_network) > calls_after_first


def test_load_index_returns_stale_copy_on_network_failure(cache_dir_env, dead_network):
    # Seed the cache with an old (expired-for-`get`) copy.
    old_index = {"built_at": "2020-01-01T00:00:00+00:00", "entries": [_entry("ListView")]}
    DocCache().set(search_mod.INDEX_CACHE_KEY, json.dumps(old_index), ttl_seconds=-1)

    result = load_index()  # rebuild fails (dead network) → stale fallback
    assert result["stale"] is True
    assert result["entries"] == old_index["entries"]


def test_load_index_failure_without_cache_never_raises(cache_dir_env, dead_network):
    result = load_index()
    assert result["entries"] == []
    assert result["stale"] is True
    assert "error" in result
