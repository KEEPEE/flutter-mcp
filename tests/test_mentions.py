"""Offline tests for flutter_mentions and the @flutter_mcp mention grammar.

Same mocking style as the rest of the suite: every network entry point
(``fetchers.fetch_flutter_class_doc`` / ``fetch_dart_class_doc`` /
``fetch_pub_package`` / ``fetch_pub_versions`` and ``search.load_index``) is
monkeypatched with canned data, so nothing touches the network.

The headline test is the phantom-entry scenario the legacy
``process_flutter_mentions`` produced: a version-constrained pub mention inside
a multi-mention text used to leave a second, empty ``flutter_widget`` entry
behind. Here the count is asserted to be exactly the number of mentions.
"""

from __future__ import annotations

import pytest

import flutter_docs_mcp.fetchers as fetchers_mod
import flutter_docs_mcp.search as search_mod
import flutter_docs_mcp.server as server_mod
from flutter_docs_mcp.mentions import parse_mentions
from flutter_docs_mcp.server import flutter_mentions

# ---------------------------------------------------------------------------
# Canned data
# ---------------------------------------------------------------------------

PROVIDER_RELEASES = [
    "1.0.0", "4.3.3", "5.0.0", "6.0.0", "6.0.1", "6.0.2",
    "6.1.0-dev.1", "6.1.0", "6.1.1", "6.1.2", "6.1.3", "6.1.4",
    "6.1.5", "6.1.5+1",
]

PROVIDER_VERSIONS = {
    "ok": True,
    "name": "provider",
    "latest": "6.1.5+1",
    "versions": list(PROVIDER_RELEASES),
    "url": "https://pub.dev/api/packages/provider",
}

DIO_VERSIONS = {
    "ok": True,
    "name": "dio",
    "latest": "5.11.1",
    "versions": ["4.0.6", "5.0.0", "5.4.0", "5.7.0", "5.9.0-dev.2", "5.11.1"],
    "url": "https://pub.dev/api/packages/dio",
}

PROVIDER_MD = "# provider\n\n" + "\n".join(
    f"Provider readme filler line {i} with enough words to be truncated by a small budget."
    for i in range(120)
)

LONG_CLASS_MD = "# class\n\n" + "\n".join(
    f"Class doc filler line {i} with enough words to be truncated by a small budget."
    for i in range(120)
)

#: pub.dev's latest release per canned package.
_LATEST = {"provider": "6.1.5+1", "dio": "5.11.1"}


def _pub_ok(name: str, version: str | None) -> dict:
    pinned = version or _LATEST.get(name, "1.0.0")
    return {
        "ok": True,
        "name": name,
        "version": pinned,
        "description": f"{name} package description.",
        "publisher": "dash-overflow.net" if name == "provider" else "flutter.cn",
        "likes": 11000 if name == "provider" else 8360,
        "pub_points": 150 if name == "provider" else 160,
        "url": f"https://pub.dev/api/packages/{name}" + (f"/versions/{pinned}" if version else ""),
        "readme_markdown": PROVIDER_MD if name == "provider" else "# dio\n\n" + LONG_CLASS_MD,
    }


FLUTTER_APPBAR_OK = {
    "ok": True,
    "url": "https://api.flutter.dev/flutter/material/AppBar-class.html",
    "title": "AppBar class",
    "markdown": "# AppBar class\n\nA Material Design app bar.\n\n## Constructors\n\nc()\n\n## Methods\n\nm()\n",
}

DART_FUTURE_OK = {
    "ok": True,
    "url": "https://api.dart.dev/dart-async/Future-class.html",
    "title": "Future",
    "markdown": "# Future\n\nA value available later.\n\n## Methods\n\nthen()\n",
}

FAKE_INDEX = {
    "built_at": "2026-01-01T00:00:00+00:00",
    "entries": [
        {"name": "Container", "library": "widgets", "kind": "class",
         "url": "https://api.flutter.dev/flutter/widgets/Container-class.html"},
    ],
}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the server's DocCache at a per-test tmp dir (never the real cache)."""
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(server_mod, "_CACHE", None)
    yield


@pytest.fixture
def canned_network(monkeypatch):
    """Every fetcher canned; returns the list of (kind, args) calls made."""
    calls: list[tuple] = []

    KNOWN_FLUTTER_CLASSES = {"AppBar", "Container", "ListView"}

    def fake_flutter(class_name, library="widgets"):
        calls.append(("flutter", class_name, library))
        if class_name in KNOWN_FLUTTER_CLASSES:
            return {
                "ok": True,
                "url": f"https://api.flutter.dev/flutter/{library}/{class_name}-class.html",
                "title": f"{class_name} class",
                "markdown": f"# {class_name} class\n\nDocs for {class_name}.\n\n{LONG_CLASS_MD}\n\n## Methods\n\nm()\n",
            }
        return {"ok": False, "error": "not found (HTTP 404)"}

    def fake_dart(class_name, library="dart-core"):
        calls.append(("dart", class_name, library))
        return dict(DART_FUTURE_OK) if class_name == "Future" else {"ok": False, "error": "not found (HTTP 404)"}

    def fake_pub(name, version=None):
        calls.append(("pub", name, version))
        if name in ("provider", "dio"):
            return _pub_ok(name, version)
        return {"ok": False, "error": "not found (HTTP 404)"}

    def fake_pub_versions(name):
        calls.append(("versions", name))
        if name == "provider":
            return dict(PROVIDER_VERSIONS)
        if name == "dio":
            return dict(DIO_VERSIONS)
        return {"ok": False, "error": "not found (HTTP 404)"}

    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", fake_flutter)
    monkeypatch.setattr(fetchers_mod, "fetch_dart_class_doc", fake_dart)
    monkeypatch.setattr(fetchers_mod, "fetch_pub_package", fake_pub)
    monkeypatch.setattr(fetchers_mod, "fetch_pub_versions", fake_pub_versions)
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: dict(FAKE_INDEX))
    return calls


# ---------------------------------------------------------------------------
# Mention grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, identifier, constraint, explicit_pub, version_requested",
    [
        ("provider", "provider", None, False, False),
        ("provider:^6.0.0", "provider", "^6.0.0", False, True),
        ("provider:6.1.5", "provider", "6.1.5", False, True),
        ("dio:>=5.0.0 <6.0.0", "dio", ">=5.0.0 <6.0.0", False, True),
        ("dio:>=5.0.0 < 6.0.0", "dio", ">=5.0.0 < 6.0.0", False, True),
        ("provider:latest", "provider", None, False, True),
        ("pub:dio", "dio", None, True, False),
        ("pub:dio:5.4.0", "dio", "5.4.0", True, True),
        ("material.AppBar", "material.AppBar", None, False, False),
        ("dart:async.Future", "dart:async.Future", None, False, False),
        ("Container", "Container", None, False, False),
    ],
)
def test_parse_mentions_forms(raw, identifier, constraint, explicit_pub, version_requested):
    records = parse_mentions(f"prose @flutter_mcp {raw} more prose")
    assert len(records) == 1
    record = records[0]
    assert record["identifier"] == identifier
    assert record["constraint"] == constraint
    assert record["explicit_pub"] is explicit_pub
    assert record["version_requested"] is version_requested
    assert record["mention"] == f"@flutter_mcp {raw}"


def test_parse_mentions_keeps_document_order_and_count():
    text = ("@flutter_mcp provider:^6.0.0 and @flutter_mcp dio then "
            "@flutter_mcp material.AppBar, plus @flutter_mcp dart:async.Future.")
    records = parse_mentions(text)
    assert [r["identifier"] for r in records] == ["provider", "dio", "material.AppBar", "dart:async.Future"]
    assert [r["start"] for r in records] == sorted(r["start"] for r in records)


def test_parse_mentions_never_swallows_the_next_mention():
    # The space-separated constraint may not eat the following mention.
    records = parse_mentions("@flutter_mcp dio:>=5.0.0 <6.0.0 @flutter_mcp provider")
    assert [r["raw"] for r in records] == ["dio:>=5.0.0 <6.0.0", "provider"]


def test_parse_mentions_stops_at_plain_prose():
    records = parse_mentions("@flutter_mcp provider is the package we use.")
    assert [r["raw"] for r in records] == ["provider"]
    assert records[0]["constraint"] is None


def test_parse_mentions_drops_trailing_punctuation():
    records = parse_mentions("see @flutter_mcp provider:^6.0.0, @flutter_mcp dio; and @flutter_mcp Container!")
    assert [r["raw"] for r in records] == ["provider:^6.0.0", "dio", "Container"]


def test_parse_mentions_bare_directive_and_no_mentions():
    assert parse_mentions("@flutter_mcp") == []
    assert parse_mentions("nothing here") == []
    assert parse_mentions("@flutter_mcpx provider") == []
    assert parse_mentions("") == []
    assert parse_mentions(None) == []


# ---------------------------------------------------------------------------
# flutter_mentions — exactly one result per mention (the phantom scenario)
# ---------------------------------------------------------------------------

PHANTOM_TEXT = (
    "Our stack: @flutter_mcp provider:^6.0.0 for state, "
    "@flutter_mcp dio:>=5.0.0 <6.0.0 for HTTP, @flutter_mcp material.AppBar for the bar."
)


def test_flutter_mentions_returns_exactly_one_entry_per_mention(canned_network):
    result = flutter_mentions(PHANTOM_TEXT, max_tokens=200)
    assert result["mentions"] == 3
    assert len(result["results"]) == 3
    # The legacy bug produced a phantom extra entry (type flutter_widget, empty
    # content) after the version-constrained pub branch.
    assert [r["mention"] for r in result["results"]] == [
        "@flutter_mcp provider:^6.0.0",
        "@flutter_mcp dio:>=5.0.0 <6.0.0",
        "@flutter_mcp material.AppBar",
    ]
    assert [r["type"] for r in result["results"]] == ["pub_package", "pub_package", "flutter_class"]
    assert all(r.get("content") for r in result["results"])
    assert not any(r.get("type") == "flutter_widget" for r in result["results"])


def test_flutter_mentions_single_constrained_mention_alone(canned_network):
    result = flutter_mentions("@flutter_mcp provider:^6.0.0", max_tokens=200)
    assert result["mentions"] == 1
    assert len(result["results"]) == 1
    assert result["results"][0]["type"] == "pub_package"


def test_flutter_mentions_failure_of_one_mention_does_not_touch_another(canned_network):
    result = flutter_mentions(
        "@flutter_mcp provider:6.1.4 then @flutter_mcp NoSuchThing then @flutter_mcp dart:async.Future",
        max_tokens=200,
    )
    assert result["mentions"] == 3
    first, second, third = result["results"]
    assert first["type"] == "pub_package" and first["version"] == "6.1.4" and first["content"]
    assert second["type"] == "not_found" and "content" not in second and second["error"]
    assert third["type"] == "dart_class" and third["content"]
    # No entry was duplicated or reordered.
    assert [r["mention"] for r in result["results"]] == [
        "@flutter_mcp provider:6.1.4",
        "@flutter_mcp NoSuchThing",
        "@flutter_mcp dart:async.Future",
    ]


def test_flutter_mentions_repeated_mention_is_repeated_result_not_stale_state(canned_network):
    result = flutter_mentions("@flutter_mcp dio @flutter_mcp dio:5.4.0", max_tokens=200)
    assert result["mentions"] == 2
    latest, pinned = result["results"]
    assert latest["version"] == "5.11.1"
    assert pinned["version"] == "5.4.0"
    assert ("pub", "dio", None) in canned_network
    assert ("pub", "dio", "5.4.0") in canned_network


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------

def test_range_constraint_reports_requested_constraint_and_resolved_version(canned_network):
    result = flutter_mentions("@flutter_mcp dio:>=5.0.0 <6.0.0", max_tokens=200)
    entry = result["results"][0]
    assert entry["requested_constraint"] == ">=5.0.0 <6.0.0"
    assert entry["version"] == "5.11.1"
    assert "constraint '>=5.0.0 <6.0.0' resolved to 5.11.1" in entry["note"]
    # The docs fetched are the resolved version's own page, not latest's.
    assert ("pub", "dio", "5.11.1") in canned_network
    assert entry["url"].endswith("/versions/5.11.1")


def test_range_excludes_prereleases(canned_network):
    entry = flutter_mentions("@flutter_mcp dio:>=5.0.0 <6.0.0", max_tokens=200)["results"][0]
    assert entry["version"] != "5.9.0-dev.2"


def test_caret_constraint_resolves_to_newest_matching_release(canned_network):
    entry = flutter_mentions("@flutter_mcp provider:^6.0.0", max_tokens=200)["results"][0]
    assert entry["version"] == "6.1.5+1"
    assert "newest published release satisfying it" in entry["note"]


def test_exact_pin_fetches_that_exact_version(canned_network):
    entry = flutter_mentions("@flutter_mcp provider:6.1.4", max_tokens=200)["results"][0]
    assert entry["version"] == "6.1.4"
    assert ("pub", "provider", "6.1.4") in canned_network
    assert "exact release 6.1.4" in entry["note"]


def test_exact_pin_that_is_not_published_is_not_found_never_another_version(canned_network):
    entry = flutter_mentions("@flutter_mcp provider:6.9.9", max_tokens=200)["results"][0]
    assert entry["type"] == "not_found"
    assert entry["version"] is None
    assert "not a published release" in entry["error"]
    assert "6.1.5" in entry["suggestion"]
    assert entry["latest_on_pub_dev"] == "6.1.5+1"
    # No docs were fetched for a made-up version.
    assert ("pub", "provider", "6.9.9") not in canned_network
    assert "content" not in entry


def test_latest_keyword_uses_pubs_latest_without_class_lookup(canned_network):
    entry = flutter_mentions("@flutter_mcp provider:latest", max_tokens=200)["results"][0]
    assert entry["type"] == "pub_package"
    assert entry["version"] == "6.1.5+1"
    assert entry["requested_constraint"] is None
    # It must not be classified as a class name (that costs two 404s).
    assert ("flutter", "provider", "widgets") not in canned_network


def test_unsupported_constraint_is_reported_as_error(canned_network):
    entry = flutter_mentions("@flutter_mcp provider:5.x", max_tokens=200)["results"][0]
    assert entry["type"] == "error"
    assert "unsupported version constraint" in entry["error"]
    assert "suggestion" in entry


def test_unknown_package_with_constraint_is_not_found(canned_network):
    entry = flutter_mentions("@flutter_mcp nosuchpkg:^1.0.0", max_tokens=200)["results"][0]
    assert entry["type"] == "not_found"
    assert "could not read the published releases" in entry["error"]


# ---------------------------------------------------------------------------
# Class mentions
# ---------------------------------------------------------------------------

def test_flutter_and_dart_class_mentions(canned_network):
    result = flutter_mentions("@flutter_mcp material.AppBar and @flutter_mcp dart:async.Future", max_tokens=500)
    flutter_entry, dart_entry = result["results"]
    assert flutter_entry["type"] == "flutter_class"
    assert flutter_entry["url"] == "https://api.flutter.dev/flutter/material/AppBar-class.html"
    assert dart_entry["type"] == "dart_class"
    assert dart_entry["url"] == "https://api.dart.dev/dart-async/Future-class.html"
    assert dart_entry["version"] is None


def test_plain_name_mention_resolves_through_the_index(canned_network):
    entry = flutter_mentions("@flutter_mcp Container", max_tokens=500)["results"][0]
    assert entry["type"] == "flutter_class"
    assert ("flutter", "Container", "widgets") in canned_network


def test_not_found_mention_suggests_flutter_search(canned_network):
    entry = flutter_mentions("@flutter_mcp NoSuchThing", max_tokens=500)["results"][0]
    assert entry["type"] == "not_found"
    assert "flutter_search('NoSuchThing')" in entry["suggestion"]


def test_invalid_dart_form_is_error_not_not_found(canned_network):
    entry = flutter_mentions("@flutter_mcp dart:async", max_tokens=500)["results"][0]
    assert entry["type"] == "error"
    assert "invalid Dart identifier" in entry["error"]


# ---------------------------------------------------------------------------
# Payload budget and tool-level behaviour
# ---------------------------------------------------------------------------

def test_max_tokens_bounds_every_payload(canned_network):
    result = flutter_mentions(PHANTOM_TEXT, max_tokens=50)
    for entry in result["results"]:
        assert entry["truncated"] is True
        assert entry["tokens"] <= 50
        assert len(entry["content"]) <= 50 * 4 + 120  # body + its own marker
        assert entry["max_tokens"] == 50
        assert entry["truncation"]["reasons"] == ["token_budget"]


def test_no_mentions_returns_empty_results(canned_network):
    result = flutter_mentions("no directives at all", max_tokens=500)
    assert result["mentions"] == 0
    assert result["results"] == []
    assert "note" in result


def test_non_string_text_is_an_error_dict(canned_network):
    result = flutter_mentions(None)
    assert "error" in result and "suggestion" in result


def test_fetcher_exception_never_escapes_the_tool(monkeypatch, canned_network):
    def boom(*args, **kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", boom)
    result = flutter_mentions("@flutter_mcp AppBar", max_tokens=200)
    assert "error" in result
    assert "unexpected error in flutter_mentions" in result["error"]
    assert "suggestion" in result
