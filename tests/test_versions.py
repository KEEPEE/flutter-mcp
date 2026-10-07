"""Offline tests for flutter_docs_mcp.versions (pub/Dart semver constraints).

Pure logic, no network: these are the rules ``flutter_mentions`` uses to answer
``provider:^6.0.0`` / ``dio:>=5.0.0 <6.0.0`` / ``provider:6.1.5`` from pub.dev's
own list of published releases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flutter_docs_mcp import versions
from flutter_docs_mcp.fetchers import parse_pub_api_versions

FIXTURES = Path(__file__).parent / "fixtures"

# A synthetic release list covering the interesting shapes: a caret boundary at
# 0.x, a pre-release, build metadata, and a next-major release.
RELEASES = [
    "0.0.3", "0.1.0", "0.2.5", "0.2.6",
    "1.0.0", "1.1.0",
    "6.1.0-dev.1", "6.1.0", "6.1.2", "6.1.5", "6.1.5+1", "6.1.10",
    "7.0.0",
]


# ---------------------------------------------------------------------------
# Version parsing / ordering
# ---------------------------------------------------------------------------

def test_parse_version_parts():
    parsed = versions.parse_version("6.1.5+1")
    assert (parsed["major"], parsed["minor"], parsed["patch"]) == (6, 1, 5)
    assert parsed["pre"] is None
    assert parsed["build"] == "1"
    assert parsed["raw"] == "6.1.5+1"
    assert versions.parse_version("6.1.0-dev.1")["pre"] == "dev.1"
    assert versions.parse_version("6.1") is None
    assert versions.parse_version("latest") is None


def test_prerelease_sorts_below_normal_release_of_same_number():
    assert versions.compare_versions("6.1.0-dev.1", "6.1.0") == -1
    assert versions.compare_versions("6.1.0", "6.1.0-dev.1") == 1
    ordered = sorted(["6.1.0", "6.1.0-dev.1", "6.1.0-dev.2", "6.0.9"], key=versions.sort_key)
    assert ordered == ["6.0.9", "6.1.0-dev.1", "6.1.0-dev.2", "6.1.0"]


def test_build_metadata_is_ignored_for_precedence():
    # 6.1.5 and 6.1.5+1 are the same precedence; only publication order breaks
    # the tie (see test_newest_matching_prefers_later_published_release).
    assert versions.compare_versions("6.1.5", "6.1.5+1") == 0


def test_numeric_prelease_identifiers_compare_numerically():
    assert versions.compare_versions("1.0.0-dev.2", "1.0.0-dev.10") == -1
    # semver §11: numeric identifiers always have lower precedence than ASCII.
    assert versions.compare_versions("1.0.0-alpha.1", "1.0.0-alpha.beta") == -1


def test_unparseable_version_sorts_last_and_never_raises():
    ordered = sorted(["1.0.0", "garbage", "0.9.0"], key=versions.sort_key)
    assert ordered == ["0.9.0", "1.0.0", "garbage"]


# ---------------------------------------------------------------------------
# Constraint parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["latest", "any", "*", ""])
def test_any_constraints(text):
    parsed = versions.parse_constraint(text)
    assert parsed is not None and parsed["any"] is True and parsed["terms"] == []


@pytest.mark.parametrize("text", ["6.1.5", "=6.1.5"])
def test_bare_version_is_an_exact_pin(text):
    parsed = versions.parse_constraint(text)
    assert parsed["exact"] is True
    assert parsed["terms"][0]["op"] == "="


def test_caret_gets_a_synthetic_upper_bound():
    assert [(t["op"], t["version"]) for t in versions.parse_constraint("^6.0.0")["terms"]] \
        == [("^", "6.0.0"), ("<", "7.0.0")]
    # Caret on 0.x is narrower: ^0.2.5 means >=0.2.5 <0.3.0.
    assert [(t["op"], t["version"]) for t in versions.parse_constraint("^0.2.5")["terms"]] \
        == [("^", "0.2.5"), ("<", "0.3.0")]
    # ^0.0.3 means >=0.0.3 <0.0.4.
    assert versions.parse_constraint("^0.0.3")["terms"][1]["version"] == "0.0.4"


def test_range_forms_space_comma_and_separated_comparator():
    for text in [">=5.0.0 <6.0.0", ">=5.0.0,<6.0.0", ">=5.0.0 < 6.0.0"]:
        parsed = versions.parse_constraint(text)
        assert parsed is not None, text
        assert [(t["op"], t["version"]) for t in parsed["terms"]] == [(">=", "5.0.0"), ("<", "6.0.0")]


@pytest.mark.parametrize("text", ["bogus", "5.x", ">=6.0.0 <", "^", "6..5"])
def test_unparseable_constraint_returns_none(text):
    assert versions.parse_constraint(text) is None


def test_constraint_mentioning_a_prerelease_allows_prereleases():
    assert versions.parse_constraint(">=6.0.0 <7.0.0-dev.99")["has_prerelease"] is True
    assert versions.parse_constraint("^6.0.0")["has_prerelease"] is False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_prereleases_are_excluded_from_ranges_unless_requested():
    caret = versions.parse_constraint("^6.0.0")
    assert versions.satisfies("6.1.0-dev.1", caret) is False
    assert versions.satisfies("6.1.0", caret) is True
    dev_range = versions.parse_constraint(">=6.0.0 <7.0.0-dev.99")
    assert versions.satisfies("6.1.0-dev.1", dev_range) is True


def test_exact_pin_is_string_exact_against_build_metadata():
    # An exact pin of 6.1.5 must not be answered with the *different* release
    # 6.1.5+1, even though their precedence is equal.
    assert versions.satisfies("6.1.5+1", versions.parse_constraint("6.1.5")) is False
    assert versions.satisfies("6.1.5", versions.parse_constraint("6.1.5")) is True


@pytest.mark.parametrize(
    "constraint, expected",
    [
        ("^0.0.3", "0.0.3"),
        ("^0.2.5", "0.2.6"),
        ("^1.0.0", "1.1.0"),
        ("^6.0.0", "6.1.10"),
        (">=6.0.0 <7.0.0", "6.1.10"),
        (">=0.0.1 <1.0.0", "0.2.6"),
        ("~>1.0.0", "1.1.0"),
        (">7.0.0", None),
        ("^8.0.0", None),
        ("6.1.6", None),
        ("latest", "7.0.0"),
    ],
)
def test_newest_matching(constraint, expected):
    parsed = versions.parse_constraint(constraint)
    assert versions.newest_matching(RELEASES, parsed) == expected


def test_newest_matching_prefers_later_published_release_on_a_tie():
    # 6.1.5 and 6.1.5+1 have equal precedence; pub.dev lists releases in
    # publication order, so the later one (6.1.5+1) is the newest.
    assert versions.newest_matching(["6.1.5", "6.1.5+1"], versions.parse_constraint("^6.0.0")) == "6.1.5+1"
    assert versions.newest_matching(["6.1.5+1", "6.1.5"], versions.parse_constraint("^6.0.0")) == "6.1.5"


def test_newest_matching_ignores_non_strings():
    assert versions.newest_matching([None, 3, "1.0.0"], versions.parse_constraint("^1.0.0")) == "1.0.0"


# ---------------------------------------------------------------------------
# parse_pub_api_versions — the release list pub.dev actually publishes
# ---------------------------------------------------------------------------

def test_parse_pub_api_versions_from_fixture():
    data = json.loads((FIXTURES / "pub_dio_api.json").read_text(encoding="utf-8"))
    parsed = parse_pub_api_versions(data)
    assert parsed["name"] == "dio"
    assert parsed["latest"] == "5.11.1"
    assert len(parsed["versions"]) > 100
    assert "5.11.1" in parsed["versions"]
    assert parsed["versions"][-1] == parsed["latest"]


def test_parse_pub_api_versions_rejects_bad_input():
    assert parse_pub_api_versions({})["versions"] == []
    assert parse_pub_api_versions({"versions": "nope"})["versions"] == []
