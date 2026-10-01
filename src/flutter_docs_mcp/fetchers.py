"""Doc fetchers for Flutter / Dart / pub.dev documentation.

Public API (signatures are stable — used by later phases):

- :func:`fetch_flutter_class_doc` — api.flutter.dev class docs as markdown
- :func:`fetch_dart_class_doc`    — api.dart.dev class docs as markdown
- :func:`fetch_pub_package`       — pub.dev package metadata + README markdown

The public ``fetch_*`` functions do the HTTP and never raise: they always
return a dict, either ``{"ok": True, ...}`` or ``{"ok": False, "error": str}``.

The HTML/JSON parsing is split into pure functions so it can be unit-tested
against local fixtures without any network access:

- :func:`parse_flutter_html` (alias :func:`parse_dartdoc_html`) — any dartdoc
  generated page (api.flutter.dev and api.dart.dev share the same layout)
- :func:`parse_pub_page_html`   — README section of a pub.dev package page
- :func:`parse_pub_page_meta`   — publisher/likes/pub_points from the page
- :func:`parse_pub_api_json`    — ``/api/packages/...`` JSON payloads

The module performs no HTTP at import time.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as _md_convert

__all__ = [
    "fetch_flutter_class_doc",
    "fetch_dart_class_doc",
    "fetch_pub_package",
    "parse_flutter_html",
    "parse_dartdoc_html",
    "parse_pub_page_html",
    "parse_pub_page_meta",
    "parse_pub_api_json",
]

USER_AGENT = "flutter-docs-mcp/0.1 (+https://github.com/KEEPEE/flutter-mcp)"
TIMEOUT = 15.0

_FLUTTER_API_BASE = "https://api.flutter.dev/flutter"
_DART_API_BASE = "https://api.dart.dev"
_PUB_API_BASE = "https://pub.dev/api/packages"
_PUB_PAGE_BASE = "https://pub.dev/packages"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(client: httpx.Client, url: str) -> tuple[str | None, str | None, str | None]:
    """GET ``url`` with one retry on transport errors.

    Returns ``(text, final_url, error)`` — exactly one of text/error is set.
    Never raises.
    """
    last_error: str | None = None
    for _attempt in range(2):
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            continue  # retry once on network-level failures
        if response.status_code == 404:
            return None, str(response.url), f"not found (HTTP 404): {url}"
        if response.status_code >= 400:
            return None, str(response.url), f"HTTP {response.status_code} from {url}"
        return response.text, str(response.url), None
    return None, url, f"network error fetching {url}: {last_error}"


# ---------------------------------------------------------------------------
# dartdoc HTML parsing (api.flutter.dev and api.dart.dev)
# ---------------------------------------------------------------------------

def _clean_dartdoc_content(content: Tag, base_url: str) -> None:
    """Strip navigation/UI noise from a dartdoc main-content node in place."""
    # Embeds and scripts never belong in markdown output.
    for tag in content.find_all(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    # Copy-link / copy-code overlay buttons inside code snippets.
    for tag in content.select("a.anchor-button, button.copy-button"):
        tag.decompose()
    # Material icon glyphs (leftover <i>/<span> icons).
    for tag in content.select("i.material-icons, span.material-symbols-outlined"):
        tag.decompose()
    # "View source code" button group next to the h1.
    external = content.select_one("#external-links")
    if external is not None:
        external.decompose()

    # Feature badges ("final", "inherited", ...) are separate inline spans that
    # markdownify would concatenate into "finalinherited"; turn each group into
    # a single emphasized, comma-joined line instead.
    for features in content.select("div.features"):
        names = [span.get_text(" ", strip=True) for span in features.find_all("span")]
        names = [n for n in names if n]
        em = BeautifulSoup(f"<em>{', '.join(names)}</em>", "lxml")
        features.replace_with(em)

    # Resolve relative links to absolute URLs. dartdoc pages carry a <base>
    # tag (e.g. href="../"); ``base_url`` is already resolved against it by
    # the caller, so plain urljoin works here.
    for a in content.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "mailto:", "javascript:")):
            continue
        a["href"] = urljoin(base_url, href)


def _resolve_base(soup: BeautifulSoup, page_url: str) -> str:
    """Return the URL that relative links on the page resolve against."""
    base_tag = soup.find("base")
    if base_tag is not None and base_tag.get("href"):
        return urljoin(page_url, base_tag["href"])
    return page_url


def _title_from_url(url: str) -> str:
    """Derive a class title from a dartdoc URL as a fallback."""
    last = url.rstrip("/").rsplit("/", 1)[-1]
    for suffix in ("-class.html", "-constant.html", "-top-level-library.html"):
        if last.endswith(suffix):
            return last[: -len(suffix)]
    return last


def _markdownify(content: Tag) -> str:
    md = _md_convert(str(content), heading_style="ATX", strip=["wbr"])
    # Normalize whitespace: drop trailing spaces, collapse 3+ newlines.
    lines = [line.rstrip() for line in md.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def parse_flutter_html(html: str, url: str) -> dict:
    """Parse a dartdoc-generated class page into clean markdown.

    Works for both api.flutter.dev and api.dart.dev (identical layout).
    Returns ``{"ok": True, "url", "title", "markdown"}`` on success or
    ``{"ok": False, "error"}`` when the expected structure is missing.
    Never raises.
    """
    try:
        soup = BeautifulSoup(html, "lxml")

        content = (
            soup.select_one("#dartdoc-main-content")
            or soup.select_one("div.main-content")
            or soup.find("main")
        )
        if content is None:
            return {"ok": False, "error": f"could not find main content on {url}"}

        h1 = content.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else _title_from_url(url)

        base_url = _resolve_base(soup, url)
        _clean_dartdoc_content(content, base_url)

        markdown = _markdownify(content)
        if not markdown.strip():
            return {"ok": False, "error": f"no documentation content found on {url}"}

        return {"ok": True, "url": url, "title": title, "markdown": markdown}
    except Exception as exc:  # defensive: parsers must never raise
        return {"ok": False, "error": f"parse error: {exc.__class__.__name__}: {exc}"}


# Alias — both API sites are generated by dartdoc.
parse_dartdoc_html = parse_flutter_html


# ---------------------------------------------------------------------------
# pub.dev parsing
# ---------------------------------------------------------------------------

def _score_number(soup: BeautifulSoup, selector: str) -> int | float | str | None:
    """Extract the numeric value from a pub.dev score block (likes/points)."""
    block = soup.select_one(selector)
    if block is None:
        return None
    value_el = block.select_one(".packages-score-value-number")
    if value_el is None:
        return None
    raw = value_el.get_text(" ", strip=True)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw  # e.g. "8.36k" — keep the human-readable string


def parse_pub_page_html(html: str) -> str:
    """Extract the README section of a pub.dev package page as markdown.

    Returns "" when the page has no README section (or cannot be parsed).
    Never raises.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        readme = (
            soup.select_one("section.detail-tab-readme-content")
            or soup.select_one("section.tab-content.markdown-body")
        )
        if readme is None:
            return ""
        for anchor in readme.find_all("a", class_="hash-link"):
            anchor.decompose()
        return _markdownify(readme)
    except Exception:  # defensive: parsers must never raise
        return ""


def parse_pub_page_meta(html: str) -> dict:
    """Extract publisher / likes / pub_points from a pub.dev package page.

    The current pub.dev API no longer ships these fields in the JSON
    response, so they are read from the rendered page instead. Each value
    is None when absent. Never raises.
    """
    result: dict = {"publisher": None, "likes": None, "pub_points": None}
    try:
        soup = BeautifulSoup(html, "lxml")
        publisher_el = soup.select_one("a.-pub-publisher")
        if publisher_el is not None:
            result["publisher"] = publisher_el.get_text(" ", strip=True) or None
        result["likes"] = _score_number(soup, ".packages-score-like")
        result["pub_points"] = _score_number(soup, ".packages-score-health")
        return result
    except Exception:  # defensive: parsers must never raise
        return result


def parse_pub_api_json(data: dict) -> dict:
    """Extract package fields from a pub.dev API JSON payload.

    Handles both shapes:
    - ``GET /api/packages/{name}``          → ``{"name", "latest": {...}}``
    - ``GET /api/packages/{name}/versions/{v}`` → flat ``{"version", "pubspec"}``

    Returns a dict with keys ``name``, ``version``, ``description``,
    ``publisher``, ``likes``, ``pub_points`` (None when absent). Never raises.
    """
    result: dict = {
        "name": None,
        "version": None,
        "description": None,
        "publisher": None,
        "likes": None,
        "pub_points": None,
    }
    try:
        if not isinstance(data, dict):
            return result

        name = data.get("name")
        version = data.get("version") if isinstance(data.get("version"), str) else None

        pubspec = data.get("pubspec")
        if not isinstance(pubspec, dict):
            latest = data.get("latest")
            if isinstance(latest, dict):
                version = version or latest.get("version")
                pubspec = latest.get("pubspec")
        if isinstance(pubspec, dict):
            name = name or pubspec.get("name")
            result["description"] = pubspec.get("description")

        latest = data.get("latest")
        scope = latest if isinstance(latest, dict) else {}
        for field in ("publisher", "likes"):
            value = data.get(field)
            if value is None:
                value = scope.get(field)
            result[field] = value
        # Points have appeared under several keys across API revisions.
        for key in ("pub_points", "pubPoints", "points", "score"):
            value = data.get(key)
            if value is None and isinstance(scope, dict):
                value = scope.get(key)
            if value is not None:
                result["pub_points"] = value
                break

        result["name"] = name
        result["version"] = version
        return result
    except Exception as exc:  # defensive: parsers must never raise
        result["error"] = f"parse error: {exc.__class__.__name__}: {exc}"
        return result


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

def fetch_flutter_class_doc(class_name: str, library: str = "widgets") -> dict:
    """Fetch a Flutter class doc from api.flutter.dev as clean markdown.

    URL pattern: ``https://api.flutter.dev/flutter/{library}/{ClassName}-class.html``
    (``library`` is used verbatim — widgets, material, cupertino, foundation, ...).

    Returns ``{"ok": True, "url", "title", "markdown"}`` on success or
    ``{"ok": False, "error"}`` on 404 / network error / timeout. Never raises.
    """
    url = f"{_FLUTTER_API_BASE}/{library}/{class_name}-class.html"
    try:
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            text, final_url, error = _http_get(client, url)
    except Exception as exc:  # defensive: never raise out of the fetcher
        return {"ok": False, "error": f"unexpected error: {exc.__class__.__name__}: {exc}"}
    if error is not None:
        return {"ok": False, "error": error}
    return parse_flutter_html(text or "", final_url or url)


def fetch_dart_class_doc(class_name: str, library: str = "dart:core") -> dict:
    """Fetch a Dart SDK class doc from api.dart.dev as clean markdown.

    URL pattern: ``https://api.dart.dev/{library}/{ClassName}-class.html`` where
    the colon in the library is replaced by a dash (``dart:async`` → ``dart-async``).

    Same return shape as :func:`fetch_flutter_class_doc`. Never raises.
    """
    lib = library.replace(":", "-")
    url = f"{_DART_API_BASE}/{lib}/{class_name}-class.html"
    try:
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            text, final_url, error = _http_get(client, url)
    except Exception as exc:  # defensive: never raise out of the fetcher
        return {"ok": False, "error": f"unexpected error: {exc.__class__.__name__}: {exc}"}
    if error is not None:
        return {"ok": False, "error": error}
    return parse_flutter_html(text or "", final_url or url)


def fetch_pub_package(package_name: str, version: str | None = None) -> dict:
    """Fetch pub.dev package metadata plus the README as markdown.

    Hits ``https://pub.dev/api/packages/{name}`` (or ``.../versions/{version}``)
    and the human page ``https://pub.dev/packages/{name}``.

    Returns on success::

        {"ok": True, "name", "version", "description", "publisher",
         "likes", "pub_points", "url", "readme_markdown"}

    ``publisher`` / ``likes`` / ``pub_points`` may be None when the API
    response lacks them (the current API omits them; they are then read from
    the package page when available). If the API call succeeds but the HTML
    page cannot be fetched, the result is still ok with an empty
    ``readme_markdown``. On API failure returns ``{"ok": False, "error"}``.
    Never raises.
    """
    api_url = f"{_PUB_API_BASE}/{package_name}"
    if version:
        api_url = f"{api_url}/versions/{version}"
    page_url = f"{_PUB_PAGE_BASE}/{package_name}"

    try:
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            text, final_url, error = _http_get(client, api_url)
            if error is not None:
                return {"ok": False, "error": error}
            try:
                data = json.loads(text or "{}")
            except ValueError as exc:
                return {"ok": False, "error": f"invalid JSON from {api_url}: {exc}"}

            # Best effort: fetch the human page for README + score fallbacks.
            readme_md = ""
            page_meta: dict = {"publisher": None, "likes": None, "pub_points": None}
            page_text, _page_final, page_error = _http_get(client, page_url)
            if page_error is None and page_text:
                readme_md = parse_pub_page_html(page_text)
                page_meta = parse_pub_page_meta(page_text)

            api_fields = parse_pub_api_json(data)
            name = api_fields.get("name") or package_name
            result = {
                "ok": True,
                "name": name,
                "version": api_fields.get("version"),
                "description": api_fields.get("description"),
                # Prefer API values; fall back to the rendered page.
                "publisher": api_fields.get("publisher") or page_meta.get("publisher"),
                "likes": api_fields.get("likes") if api_fields.get("likes") is not None else page_meta.get("likes"),
                "pub_points": api_fields.get("pub_points") if api_fields.get("pub_points") is not None else page_meta.get("pub_points"),
                "url": final_url or api_url,
                "readme_markdown": readme_md,
            }
            return result
    except Exception as exc:  # defensive: never raise out of the fetcher
        return {"ok": False, "error": f"unexpected error: {exc.__class__.__name__}: {exc}"}
