"""Offline tests for the MCP tools in flutter_docs_mcp.server.

The tool functions are plain (synchronous) functions decorated with
``@mcp.tool()``, so they can be called directly from here. Every network entry
point — ``fetchers.fetch_flutter_class_doc`` / ``fetch_dart_class_doc`` /
``fetch_pub_package`` and ``search.load_index`` / ``search.search`` — is
monkeypatched with canned data, so nothing touches the network. The server's
DocCache is pointed at a per-test tmp dir via FLUTTER_DOCS_MCP_CACHE_DIR.
"""

from __future__ import annotations

import pytest

import flutter_docs_mcp.fetchers as fetchers_mod
import flutter_docs_mcp.search as search_mod
import flutter_docs_mcp.server as server_mod
from flutter_docs_mcp.server import (
    _apply_topic,
    _truncate_markdown,
    flutter_docs,
    flutter_search,
    flutter_status,
    health_check,
    pub_package,
)


# ---------------------------------------------------------------------------
# Canned data
# ---------------------------------------------------------------------------

TOPIC_MD = """# FooClass

A description line about FooClass.

## Constructors

ctor stuff here

## Methods

method one does things
method two does other things

## Properties

property details go here
"""

LONG_MD = "# BigClass\n\n" + "\n".join(
    f"This is filler line {i} with enough words to push the token estimate well past any small limit."
    for i in range(300)
)

FLUTTER_OK = {
    "ok": True,
    "url": "https://api.flutter.dev/flutter/widgets/ListView-class.html",
    "title": "ListView",
    "markdown": TOPIC_MD,
}

DART_OK = {
    "ok": True,
    "url": "https://api.dart.dev/dart-async/Future-class.html",
    "title": "Future",
    "markdown": "# Future\n\nA value available later.\n\n## Methods\n\nthen()\n",
}

PUB_OK = {
    "ok": True,
    "name": "dio",
    "version": "5.4.0",
    "description": "A powerful HTTP networking package.",
    "publisher": None,
    "likes": 123,
    "pub_points": 140,
    "url": "https://pub.dev/api/packages/dio",
    "readme_markdown": "# dio\n\nHTTP client readme body.\n",
}

FAKE_INDEX = {
    "built_at": "2026-01-01T00:00:00+00:00",
    "entries": [
        {"name": "ListView", "library": "widgets", "kind": "class",
         "url": "https://api.flutter.dev/flutter/widgets/ListView-class.html"},
        {"name": "TextField", "library": "material", "kind": "class",
         "url": "https://api.flutter.dev/flutter/material/TextField-class.html"},
    ],
}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the server's DocCache at a per-test tmp dir (never the real cache)."""
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(server_mod, "_CACHE", None)
    yield


# ---------------------------------------------------------------------------
# flutter_docs — identifier resolution
# ---------------------------------------------------------------------------

def test_flutter_docs_pub_identifier(monkeypatch):
    calls = []

    def fake_pub(name, version=None):
        calls.append((name, version))
        return dict(PUB_OK)

    monkeypatch.setattr(fetchers_mod, "fetch_pub_package", fake_pub)
    result = flutter_docs("pub:dio")
    assert calls == [("dio", None)]
    assert result["type"] == "pub_package"
    assert result["identifier"] == "pub:dio"
    assert "A powerful HTTP networking package." in result["content"]
    assert "HTTP client readme body." in result["content"]
    assert result["truncated"] is False
    assert result["cached"] is False


def test_flutter_docs_pub_identifier_with_version(monkeypatch):
    calls = []

    def fake_pub(name, version=None):
        calls.append((name, version))
        return dict(PUB_OK)

    monkeypatch.setattr(fetchers_mod, "fetch_pub_package", fake_pub)
    result = flutter_docs("pub:dio:5.4.0")
    assert calls == [("dio", "5.4.0")]
    assert result["type"] == "pub_package"


def test_flutter_docs_dart_identifier(monkeypatch):
    calls = []

    def fake_dart(class_name, library="dart:core"):
        calls.append((class_name, library))
        return dict(DART_OK)

    monkeypatch.setattr(fetchers_mod, "fetch_dart_class_doc", fake_dart)
    result = flutter_docs("dart:async.Future")
    assert calls == [("Future", "dart:async")]
    assert result["type"] == "dart_class"
    assert result["title"] == "Future"


def test_flutter_docs_explicit_library(monkeypatch):
    calls = []

    def fake_flutter(class_name, library="widgets"):
        calls.append((class_name, library))
        return dict(FLUTTER_OK)

    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", fake_flutter)
    result = flutter_docs("material.AppBar")
    assert calls == [("AppBar", "material")]
    assert result["type"] == "flutter_class"


def test_flutter_docs_plain_name_via_index(monkeypatch):
    calls = []

    def fake_flutter(class_name, library="widgets"):
        calls.append((class_name, library))
        return dict(FLUTTER_OK)

    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", fake_flutter)
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: FAKE_INDEX)
    result = flutter_docs("ListView")
    assert calls == [("ListView", "widgets")]
    assert result["type"] == "flutter_class"
    assert result["url"].startswith("https://api.flutter.dev/flutter/widgets/")


def test_flutter_docs_plain_name_index_miss_falls_to_pub(monkeypatch):
    def fake_flutter(class_name, library="widgets"):
        return {"ok": False, "error": f"not found (HTTP 404): {library}/{class_name}"}

    calls = []

    def fake_pub(name, version=None):
        calls.append((name, version))
        return dict(PUB_OK)

    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", fake_flutter)
    monkeypatch.setattr(fetchers_mod, "fetch_pub_package", fake_pub)
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: FAKE_INDEX)
    # "dio" is not in the fake index → widgets/material miss → pub hit.
    result = flutter_docs("dio")
    assert calls == [("dio", None)]
    assert result["type"] == "pub_package"


def test_flutter_docs_unknown_name_returns_error(monkeypatch):
    monkeypatch.setattr(
        fetchers_mod, "fetch_flutter_class_doc",
        lambda c, l="widgets": {"ok": False, "error": "not found (HTTP 404)"},
    )
    monkeypatch.setattr(
        fetchers_mod, "fetch_dart_class_doc",
        lambda c, l="dart:core": {"ok": False, "error": "not found (HTTP 404)"},
    )
    monkeypatch.setattr(
        fetchers_mod, "fetch_pub_package",
        lambda n, v=None: {"ok": False, "error": "not found (HTTP 404)"},
    )
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: FAKE_INDEX)
    result = flutter_docs("DefinitelyNotARealClassXYZ123")
    assert isinstance(result, dict)
    assert "error" in result
    assert "suggestion" in result
    assert "flutter_search" in result["suggestion"]


# ---------------------------------------------------------------------------
# flutter_docs — truncation
# ---------------------------------------------------------------------------

def test_flutter_docs_truncation(monkeypatch):
    monkeypatch.setattr(
        fetchers_mod, "fetch_flutter_class_doc",
        lambda c, l="widgets": {"ok": True, "url": "u", "title": "BigClass", "markdown": LONG_MD},
    )
    result = flutter_docs("material.BigClass", max_tokens=100)
    assert result["truncated"] is True
    assert "[truncated: showing ~" in result["content"]
    assert result["content"].endswith("estimated tokens]")
    # The body (minus the trailing note) must respect the char budget.
    body = result["content"].split("\n\n[truncated:")[0]
    assert len(body) <= 100 * 4


def test_flutter_docs_no_truncation_when_small(monkeypatch):
    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", lambda c, l="widgets": dict(FLUTTER_OK))
    result = flutter_docs("material.FooClass")
    assert result["truncated"] is False
    assert "[truncated:" not in result["content"]


# ---------------------------------------------------------------------------
# flutter_docs — topic filter
# ---------------------------------------------------------------------------

def test_flutter_docs_topic_filter(monkeypatch):
    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", lambda c, l="widgets": dict(FLUTTER_OK))
    result = flutter_docs("material.FooClass", topic="methods")
    content = result["content"]
    assert "# FooClass" in content
    assert "A description line about FooClass." in content
    assert "## Methods" in content
    assert "method one does things" in content
    assert "## Properties" not in content
    assert "ctor stuff here" not in content
    assert "note" not in result


def test_flutter_docs_topic_no_match_returns_full_with_note(monkeypatch):
    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", lambda c, l="widgets": dict(FLUTTER_OK))
    result = flutter_docs("material.FooClass", topic="does-not-exist")
    assert "## Methods" in result["content"]
    assert "## Properties" in result["content"]
    assert "note" in result
    assert "Methods" in result["note"]
    assert "Properties" in result["note"]


# ---------------------------------------------------------------------------
# flutter_docs — caching + error paths
# ---------------------------------------------------------------------------

def test_flutter_docs_uses_cache(monkeypatch):
    calls = []

    def fake_flutter(class_name, library="widgets"):
        calls.append((class_name, library))
        return dict(FLUTTER_OK)

    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", fake_flutter)
    first = flutter_docs("material.FooClass")
    second = flutter_docs("material.FooClass")
    assert len(calls) == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["content"] == first["content"]


def test_flutter_docs_all_fetchers_fail(monkeypatch):
    monkeypatch.setattr(
        fetchers_mod, "fetch_flutter_class_doc",
        lambda c, l="widgets": {"ok": False, "error": "not found (HTTP 404)"},
    )
    monkeypatch.setattr(
        fetchers_mod, "fetch_dart_class_doc",
        lambda c, l="dart:core": {"ok": False, "error": "not found (HTTP 404)"},
    )
    monkeypatch.setattr(
        fetchers_mod, "fetch_pub_package",
        lambda n, v=None: {"ok": False, "error": "not found (HTTP 404)"},
    )
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: FAKE_INDEX)
    # Index hit → fetch fails → widgets/material fail → pub fails → error dict.
    result = flutter_docs("ListView")
    assert isinstance(result, dict)
    assert "error" in result
    assert "suggestion" in result


def test_flutter_docs_dart_fetch_fail(monkeypatch):
    monkeypatch.setattr(
        fetchers_mod, "fetch_dart_class_doc",
        lambda c, l="dart:core": {"ok": False, "error": "not found (HTTP 404)"},
    )
    result = flutter_docs("dart:async.Future")
    assert "error" in result
    assert "suggestion" in result


def test_flutter_docs_empty_identifier():
    result = flutter_docs("   ")
    assert "error" in result
    assert "suggestion" in result


# ---------------------------------------------------------------------------
# flutter_search
# ---------------------------------------------------------------------------

def test_flutter_search_success(monkeypatch):
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: {**FAKE_INDEX, "stale": True})
    canned = [{
        "name": "TextField", "library": "material", "kind": "class",
        "url": "https://api.flutter.dev/flutter/material/TextField-class.html",
        "score": 0.95,
    }]
    monkeypatch.setattr(search_mod, "search", lambda q, limit=8, index=None: list(canned))
    result = flutter_search("text field", limit=3)
    assert result["query"] == "text field"
    assert result["count"] == 1
    assert result["results"] == canned
    assert result["index_stale"] is True


def test_flutter_search_index_unavailable(monkeypatch):
    def boom(**kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(search_mod, "load_index", boom)
    result = flutter_search("list view")
    assert isinstance(result, dict)
    assert "error" in result
    assert "suggestion" in result


def test_flutter_search_empty_query():
    result = flutter_search("")
    assert "error" in result
    assert "suggestion" in result


# ---------------------------------------------------------------------------
# pub_package
# ---------------------------------------------------------------------------

def test_pub_package_success(monkeypatch):
    monkeypatch.setattr(fetchers_mod, "fetch_pub_package", lambda n, v=None: dict(PUB_OK))
    result = pub_package("dio")
    assert result["name"] == "dio"
    assert result["version"] == "5.4.0"
    assert result["description"].startswith("A powerful HTTP")
    assert result["publisher"] is None
    assert result["likes"] == 123
    assert result["pub_points"] == 140
    assert result["url"] == "https://pub.dev/api/packages/dio"
    assert "HTTP client readme body." in result["readme"]
    assert result["truncated"] is False
    assert result["cached"] is False


def test_pub_package_truncation(monkeypatch):
    long_readme = "\n".join(
        f"line {i} of a very long readme with plenty of words to fill the buffer up"
        for i in range(400)
    )
    monkeypatch.setattr(
        fetchers_mod, "fetch_pub_package",
        lambda n, v=None: {**PUB_OK, "readme_markdown": long_readme},
    )
    result = pub_package("dio", max_tokens=100)
    assert result["truncated"] is True
    assert result["readme"].endswith("estimated tokens]")


def test_pub_package_error(monkeypatch):
    monkeypatch.setattr(
        fetchers_mod, "fetch_pub_package",
        lambda n, v=None: {"ok": False, "error": "not found (HTTP 404)"},
    )
    result = pub_package("no-such-pkg")
    assert "error" in result
    assert "suggestion" in result


# ---------------------------------------------------------------------------
# flutter_status (with a fake httpx client — fully offline)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = "ok"


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, url):
        return _FakeResponse(200)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_flutter_status_ok(monkeypatch):
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: FAKE_INDEX)
    monkeypatch.setattr(server_mod.httpx, "Client", _FakeClient)
    result = flutter_status()
    assert result["server"] == "flutter-docs-mcp"
    checks = result["checks"]
    assert set(checks) == {"search_index", "cache", "api_flutter_dev", "pub_dev"}
    assert checks["search_index"]["status"] == "ok"
    assert checks["search_index"]["entries"] == 2
    assert checks["search_index"]["stale"] is False
    assert checks["cache"]["status"] == "ok"
    assert set(checks["cache"]) == {"status", "entries", "expired"}
    assert checks["api_flutter_dev"] == {"status": "ok", "http_status": 200}
    assert checks["pub_dev"] == {"status": "ok", "http_status": 200}
    assert result["overall"] == "ok"


def test_flutter_status_degraded_on_probe_failure(monkeypatch):
    class _DownClient(_FakeClient):
        def get(self, url):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(search_mod, "load_index", lambda **kw: FAKE_INDEX)
    monkeypatch.setattr(server_mod.httpx, "Client", _DownClient)
    result = flutter_status()
    assert result["checks"]["api_flutter_dev"] == {"status": "error", "http_status": None}
    assert result["checks"]["pub_dev"]["status"] == "error"
    assert result["checks"]["search_index"]["status"] == "ok"
    assert result["overall"] == "degraded"


# ---------------------------------------------------------------------------
# Pure helpers + health_check
# ---------------------------------------------------------------------------

def test_apply_topic_ignores_headings_in_code_fences():
    md = (
        "# T\n\ndesc\n\n"
        "## Examples\n\n"
        "```bash\n"
        "# shell comment that looks like a heading\n"
        "echo hi\n"
        "```\n\n"
        "real example text\n\n"
        "## Methods\n\nm()\n"
    )
    filtered, headings = _apply_topic(md, "methods")
    assert "## Methods" in filtered
    assert "m()" in filtered
    assert "## Examples" not in filtered
    assert "shell comment" not in filtered


def test_truncate_markdown_respects_line_boundary():
    text = "\n".join(f"line {i}" for i in range(500))
    out, truncated = _truncate_markdown(text, 10)
    assert truncated is True
    body = out.split("\n\n[truncated:")[0]
    assert len(body) <= 40
    # Cut at a line boundary: body ends on a complete "line N".
    last_line = body.rstrip().rsplit("\n", 1)[-1]
    assert last_line.startswith("line ")


def test_truncate_markdown_disabled_for_non_positive_budget():
    text = "x" * 1000
    out, truncated = _truncate_markdown(text, 0)
    assert truncated is False
    assert out == text


def test_health_check():
    result = health_check()
    assert result["status"] == "ok"
    assert result["server"] == "flutter-docs-mcp"
    assert "version" in result
