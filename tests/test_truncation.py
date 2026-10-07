"""Offline tests for truncation truthfulness in flutter_docs / pub_package.

The rule these tests pin down: ``truncated`` is true whenever the returned
content is **not the whole page** — a token-budget cut, a topic filter, or both
— and every token number quoted in the response describes the payload that was
actually returned and the page it came from.

Mocking style matches the rest of the suite: the fetchers are monkeypatched
with canned markdown, so nothing touches the network.
"""

from __future__ import annotations

import pytest

import flutter_docs_mcp.fetchers as fetchers_mod
import flutter_docs_mcp.search as search_mod
import flutter_docs_mcp.server as server_mod
from flutter_docs_mcp.server import (
    _apply_topic,
    _fit_to_budget,
    _truncate_markdown,
    flutter_docs,
    pub_package,
)

# ---------------------------------------------------------------------------
# Canned page: an AppBar-shaped dartdoc page with several member sections
# ---------------------------------------------------------------------------

_FILLER = "\n".join(
    f"Member documentation line {i} for AppBar, long enough to matter for a token budget."
    for i in range(60)
)

APPBAR_MD = f"""# AppBar class

A Material Design app bar.

## Constructors

AppBar constructor documentation.
{ _FILLER[:400] }

## Properties

Properties of the app bar.
{_FILLER}

## Methods

Methods of the app bar.
{_FILLER}

## Operators

Operators.
{_FILLER[:200]}

## Static Methods

Static methods.
{_FILLER[:200]}
"""

APPBAR_OK = {
    "ok": True,
    "url": "https://api.flutter.dev/flutter/material/AppBar-class.html",
    "title": "AppBar class",
    "markdown": APPBAR_MD,
}

PUB_OK = {
    "ok": True,
    "name": "dio",
    "version": "5.11.1",
    "description": "A powerful HTTP networking package.",
    "publisher": "flutter.cn",
    "likes": 8360,
    "pub_points": 160,
    "url": "https://pub.dev/api/packages/dio",
    "readme_markdown": "# dio\n\n" + _FILLER + "\n" + _FILLER,
}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the server's DocCache at a per-test tmp dir (never the real cache)."""
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(server_mod, "_CACHE", None)
    yield


@pytest.fixture(autouse=True)
def canned_network(monkeypatch):
    monkeypatch.setattr(fetchers_mod, "fetch_flutter_class_doc", lambda name, library="widgets": dict(APPBAR_OK))
    monkeypatch.setattr(fetchers_mod, "fetch_dart_class_doc", lambda name, library="dart-core": dict(APPBAR_OK))
    monkeypatch.setattr(fetchers_mod, "fetch_pub_package", lambda name, version=None: dict(PUB_OK))
    monkeypatch.setattr(search_mod, "load_index", lambda **kw: {"built_at": "2026-01-01T00:00:00+00:00", "entries": []})


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("budget", [100, 500, 1000, 2000])
def test_budgeted_fetch_is_truthful(budget):
    result = flutter_docs("AppBar", max_tokens=budget)
    content = result["content"]
    body, marker = content.split("\n\n[truncated: ")

    assert result["truncated"] is True
    assert result["max_tokens"] == budget
    # The marker quotes the same numbers the response reports…
    shown, source = marker.removeprefix("showing ~").split(" of ~")
    assert int(shown) == result["tokens"] == len(body) // 4
    assert int(source.split()[0]) == result["source_tokens"] == len(APPBAR_MD) // 4
    # …and the source number is the *whole page*, never the already-cut size.
    assert result["source_tokens"] > result["tokens"]
    # The body alone always fits; the marker's own cost is disclosed.
    assert len(body) <= budget * 4
    detail = result["truncation"]
    assert detail["reasons"] == ["token_budget"]
    assert detail["tokens_payload"] == len(content) // 4
    assert detail["tokens_returned"] == result["tokens"]
    assert detail["tokens_source"] == result["source_tokens"]
    # A budget that can hold the marker pays for it out of the same budget.
    if budget >= 200:
        assert len(content) <= budget * 4


def test_no_truncation_when_the_page_fits_the_budget():
    result = flutter_docs("AppBar", max_tokens=(len(APPBAR_MD) + 3) // 4)
    assert result["truncated"] is False
    assert result["content"] == APPBAR_MD
    assert result["tokens"] == result["source_tokens"] == len(APPBAR_MD) // 4
    assert "truncation" not in result
    assert "[truncated:" not in result["content"]


@pytest.mark.parametrize("budget", [0, -5, None])
def test_non_positive_or_missing_budget_disables_truncation(budget):
    result = flutter_docs("AppBar", max_tokens=budget)
    assert result["truncated"] is False
    assert result["content"] == APPBAR_MD
    assert result["max_tokens"] is None
    assert "truncation" not in result


def test_tiny_budget_keeps_the_body_and_discloses_the_marker_cost():
    result = flutter_docs("AppBar", max_tokens=10)
    detail = result["truncation"]
    assert result["truncated"] is True
    assert result["tokens"] <= 10
    # The marker cannot fit in 40 chars, so the payload is bigger than the
    # budget — and the response says so instead of hiding it.
    assert detail["tokens_payload"] > result["tokens"]
    assert detail["explanation"].startswith("token budget 10:")


# ---------------------------------------------------------------------------
# Topic filter: a filter is a truncation too
# ---------------------------------------------------------------------------

def test_topic_filter_marks_truncated_and_names_what_was_dropped():
    result = flutter_docs("AppBar", topic="properties")
    assert result["truncated"] is True
    detail = result["truncation"]
    assert detail["reasons"] == ["topic_filter"]
    assert detail["budget_cut"] is False
    assert detail["topic_filtered"] is True
    assert "Methods" in detail["sections_dropped"]
    assert "Constructors" in detail["sections_dropped"]
    assert "Static Methods" in detail["sections_dropped"]
    assert "Properties" not in detail["sections_dropped"]
    # The kept section is really there and the dropped ones really are not.
    assert "## Properties" in result["content"]
    assert "## Methods" not in result["content"]
    # Numbers still describe the whole page vs what was returned.
    assert result["source_tokens"] == len(APPBAR_MD) // 4
    assert result["tokens"] == len(result["content"]) // 4
    assert result["tokens"] < result["source_tokens"]
    # "note" stays reserved for the no-match case.
    assert "note" not in result


def test_topic_filter_plus_budget_reports_both_reasons_and_the_page_size():
    result = flutter_docs("AppBar", topic="properties", max_tokens=300)
    detail = result["truncation"]
    assert result["truncated"] is True
    assert detail["reasons"] == ["topic_filter", "token_budget"]
    # The old bug: the marker quoted the *filtered* size as the source. The
    # source is the page, so the reader sees how much the filter removed too.
    assert result["source_tokens"] == len(APPBAR_MD) // 4
    assert "topic 'properties' dropped" in detail["explanation"]
    assert "token budget 300" in detail["explanation"]


def test_topic_that_matches_nothing_is_not_reported_as_truncated():
    result = flutter_docs("AppBar", topic="nonexistent")
    assert result["truncated"] is False
    assert "truncation" not in result
    assert "no section matching topic 'nonexistent'" in result["note"]
    assert "Properties" in result["note"]
    assert result["content"] == APPBAR_MD


# ---------------------------------------------------------------------------
# pub_package uses the same truthful numbers
# ---------------------------------------------------------------------------

def test_pub_package_readme_truncation_is_truthful():
    result = pub_package("dio", max_tokens=500)
    readme = result["readme"]
    body, marker = readme.split("\n\n[truncated: ")
    assert result["truncated"] is True
    shown, source = marker.removeprefix("showing ~").split(" of ~")
    assert int(shown) == result["tokens"] == len(body) // 4
    assert int(source.split()[0]) == result["source_tokens"] == len(PUB_OK["readme_markdown"]) // 4
    assert result["truncation"]["reasons"] == ["token_budget"]
    assert len(body) <= 500 * 4


def test_pub_package_without_truncation_reports_no_truncation_block():
    # ceil(len/4) so the char budget really covers every character of the readme.
    result = pub_package("dio", max_tokens=(len(PUB_OK["readme_markdown"]) + 3) // 4)
    assert result["truncated"] is False
    assert "truncation" not in result
    assert result["readme"] == PUB_OK["readme_markdown"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def test_fit_to_budget_reports_its_own_decision():
    text = "\n".join(f"line {i}" for i in range(200))
    content, detail = _fit_to_budget(text, 10, len(text) // 4)
    assert detail["budget_cut"] is True
    assert detail["max_tokens"] == 10
    assert detail["source_tokens"] == len(text) // 4
    assert content.endswith("estimated tokens]")
    assert len(content.split("\n\n[truncated: ")[0]) <= 40

    same, detail = _fit_to_budget(text, 100000)
    assert same == text
    assert detail == {"budget_cut": False, "tokens": len(text) // 4,
                      "source_tokens": len(text) // 4, "max_tokens": 100000}


def test_two_value_wrappers_stay_compatible():
    text = "\n".join(f"line {i}" for i in range(200))
    cut, truncated = _truncate_markdown(text, 10)
    assert truncated is True
    assert cut.startswith("line 0")
    assert cut.split("\n\n[truncated: ")[0].endswith("line 4")
    assert _truncate_markdown(text, 0) == (text, False)

    filtered, headings = _apply_topic(APPBAR_MD, "methods")
    assert headings is None
    assert "## Methods" in filtered
    assert "## Properties" not in filtered

    full, headings = _apply_topic(APPBAR_MD, "nope")
    assert full == APPBAR_MD
    assert "Properties" in headings
