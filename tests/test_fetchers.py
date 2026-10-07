"""Offline tests for flutter_docs_mcp.fetchers.

These tests parse local fixture files (real pages downloaded from
api.flutter.dev, api.dart.dev and pub.dev) — no network access is used.
The public fetch_* functions are covered by the live smoke test instead.
"""

import json
from pathlib import Path

import flutter_docs_mcp.fetchers as fetchers_mod
from flutter_docs_mcp.fetchers import (
    parse_flutter_html,
    parse_pub_api_json,
    parse_pub_page_html,
    parse_pub_page_meta,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestFlutterListViewFixture:
    URL = "https://api.flutter.dev/flutter/widgets/ListView-class.html"

    def setup_method(self):
        self.result = parse_flutter_html(_read("flutter_listview.html"), self.URL)

    def test_ok_and_title(self):
        assert self.result["ok"] is True
        assert "ListView" in self.result["title"]

    def test_markdown_nonempty(self):
        assert len(self.result["markdown"]) > 1000

    def test_known_phrase_from_real_doc(self):
        assert "A scrollable list of widgets arranged linearly." in self.result["markdown"]

    def test_no_nav_or_ui_junk(self):
        # These strings exist in the raw fixture HTML (header theme toggle,
        # snippet copy buttons) but must not leak into the markdown.
        md = self.result["markdown"]
        for junk in ("dark_mode", "light_mode", "content_copy", "Copy link to clipboard"):
            assert junk not in md, f"nav/UI junk leaked into markdown: {junk!r}"

    def test_code_blocks_preserved(self):
        # The fixture contains 6 <pre> code samples; each must become a fenced block.
        assert self.result["markdown"].count("```") >= 12

    def test_doc_sections_present(self):
        md = self.result["markdown"]
        for section in ("## Constructors", "## Properties", "## Methods"):
            assert section in md

    def test_relative_links_resolved_to_absolute(self):
        assert (
            "[itemExtent](https://api.flutter.dev/flutter/widgets/ListView/itemExtent.html)"
            in self.result["markdown"]
        )


class TestDartObjectFixture:
    URL = "https://api.dart.dev/dart-core/Object-class.html"

    def setup_method(self):
        self.result = parse_flutter_html(_read("dart_object.html"), self.URL)

    def test_ok_and_title(self):
        assert self.result["ok"] is True
        assert "Object" in self.result["title"]

    def test_markdown_nonempty(self):
        assert len(self.result["markdown"]) > 500


class TestPubDioFixture:
    def setup_method(self):
        self.api = json.loads(_read("pub_dio_api.json"))
        self.readme = parse_pub_page_html(_read("pub_dio_page.html"))
        self.meta = parse_pub_page_meta(_read("pub_dio_page.html"))

    def test_api_gives_version_and_description(self):
        parsed = parse_pub_api_json(self.api)
        assert parsed["name"] == "dio"
        assert parsed["version"]
        assert parsed["description"]
        assert "HTTP networking package" in parsed["description"]

    def test_readme_nonempty_and_mentions_dio(self):
        assert len(self.readme) > 1000
        assert "dio" in self.readme.lower()

    def test_page_extras(self):
        # The current pub.dev API no longer ships publisher/likes/points, so
        # they are read from the rendered package page.
        assert self.meta["publisher"] == "flutter.cn"
        assert self.meta["pub_points"] == 160
        assert self.meta["likes"] is not None


class TestParserRobustness:
    def test_garbage_html_does_not_raise(self):
        result = parse_flutter_html(
            "<html><body>nothing useful</body></html>", "https://example.com/x-class.html"
        )
        assert result["ok"] is False
        assert "error" in result

    def test_empty_pub_page(self):
        empty = "<html><body></body></html>"
        assert parse_pub_page_html(empty) == ""
        meta = parse_pub_page_meta(empty)
        assert meta["publisher"] is None
        assert meta["likes"] is None
        assert meta["pub_points"] is None

    def test_version_specific_api_shape(self):
        # GET /api/packages/dio/versions/{v} returns a flat object (no "latest").
        data = {"version": "5.4.0", "pubspec": {"name": "dio", "description": "desc"}}
        parsed = parse_pub_api_json(data)
        assert parsed["name"] == "dio"
        assert parsed["version"] == "5.4.0"
        assert parsed["description"] == "desc"


class TestFetchPubVersions:
    """fetch_pub_versions with the HTTP layer canned — no network.

    This is the release list a version-constrained ``@flutter_mcp`` mention
    resolves against, so its failure modes matter as much as its success path.
    """

    URL = "https://pub.dev/api/packages/dio"

    @staticmethod
    def _patch(monkeypatch, text=None, error=None, status=200, raise_with=None):
        calls = []

        def fake_http_get(client, url, **kwargs):
            calls.append((url, kwargs.get("budget_scope")))
            if raise_with is not None:
                raise raise_with
            return (text, url, error, {"status_code": status, "from_cache": False,
                                       "validators": {}, "blocked_by_robots": False})

        monkeypatch.setattr(fetchers_mod, "_http_get", fake_http_get)
        return calls

    def test_success_uses_one_request_and_returns_the_release_list(self, monkeypatch):
        calls = self._patch(monkeypatch, text=_read("pub_dio_api.json"))
        result = fetchers_mod.fetch_pub_versions("dio")
        assert calls == [(self.URL, "fetch:pub.dev")]  # exactly one GET, budgeted per host
        assert result["ok"] is True
        assert result["name"] == "dio"
        assert result["latest"] == "5.11.1"
        assert "5.4.0" in result["versions"]
        assert result["url"] == self.URL

    def test_404_is_reported_not_raised(self, monkeypatch):
        self._patch(monkeypatch, error="not found (HTTP 404): https://pub.dev/api/packages/nope")
        result = fetchers_mod.fetch_pub_versions("nope")
        assert result["ok"] is False
        assert "404" in result["error"]

    def test_invalid_json_is_reported_not_raised(self, monkeypatch):
        self._patch(monkeypatch, text="<html>not json</html>")
        result = fetchers_mod.fetch_pub_versions("dio")
        assert result["ok"] is False
        assert "invalid JSON" in result["error"]

    def test_empty_version_list_is_a_failure_not_an_empty_success(self, monkeypatch):
        self._patch(monkeypatch, text='{"name": "dio"}')
        result = fetchers_mod.fetch_pub_versions("dio")
        assert result["ok"] is False
        assert "no version list" in result["error"]

    def test_transport_exception_is_reported_not_raised(self, monkeypatch):
        self._patch(monkeypatch, raise_with=RuntimeError("socket died"))
        result = fetchers_mod.fetch_pub_versions("dio")
        assert result["ok"] is False
        assert "unexpected error" in result["error"]
