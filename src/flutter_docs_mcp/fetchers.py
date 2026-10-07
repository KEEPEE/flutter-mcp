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

Politeness: every HTTP request (including the single transport retry) goes
through :mod:`flutter_docs_mcp.politeness` — robots.txt rules, per-host
throttle, ``Retry-After``/backoff, conditional GET and a per-call request
budget. The layer never raises and never changes the return shape; a
robots-disallowed URL comes back as ``{"ok": False, "error": "… blocked by
robots.txt …"}``. Opt out with ``FLUTTER_DOCS_MCP_POLITENESS_DISABLED=1``.
"""

from __future__ import annotations

import json
import re
import threading
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as _md_convert

from .cache import DocCache
from .politeness import Politeness, default_robots_db_path

__all__ = [
    "fetch_flutter_class_doc",
    "fetch_dart_class_doc",
    "fetch_pub_package",
    "parse_flutter_html",
    "parse_dartdoc_html",
    "parse_pub_page_html",
    "parse_pub_page_meta",
    "parse_pub_api_json",
    "parse_pub_api_versions",
    "fetch_pub_versions",
    "get_politeness",
    "set_politeness",
]

USER_AGENT = "flutter-docs-mcp/0.2 (+https://github.com/KEEPEE/flutter-mcp)"
TIMEOUT = 15.0

_FLUTTER_API_BASE = "https://api.flutter.dev/flutter"
_DART_API_BASE = "https://api.dart.dev"
_PUB_API_BASE = "https://pub.dev/api/packages"
_PUB_PAGE_BASE = "https://pub.dev/packages"

#: The only hosts this module ever contacts. Handed to the politeness layer as
#: an allowlist so an unexpected redirect or a malformed identifier can never
#: turn a docs lookup into a request to somebody else's site.
ALLOWED_HOSTS = frozenset({"api.flutter.dev", "api.dart.dev", "pub.dev"})

#: Hard cap on **requests on the wire** one public ``fetch_*`` call may make to
#: a single host.  Since A8 F3 a budget unit *is* a request: the robots.txt
#: fetch, the first try, every ``429``/transport retry and every redirect hop
#: all pay from it.  Measured live in the A9 smoke: a cold ``pub_package`` costs
#: 3 units (robots.txt + API JSON + package page) and a cold class doc 2
#: (robots.txt + page), so the old 4 left a single unit of slack — one
#: ``302`` on the package page and the README was silently dropped.  6 covers
#: robots + both pub.dev URLs + a two-hop chain + one retry.
FETCH_BUDGET_LIMIT = 6

#: TTL for the raw body + validators the fetcher keeps for conditional GET.
#: Matches the server's docs TTL so a revalidation window always exists.
REVALIDATION_TTL_SECONDS = 7 * 24 * 3600

_politeness: Politeness | None = None
_politeness_lock = threading.Lock()


def get_politeness() -> Politeness:
    """Process-wide politeness layer (created on first use).

    The robots cache is a SQLite file next to ``cache.db`` so it survives
    restarts (1 robots request per host per 7 days, not per call). If that
    directory is not writable the layer falls back to a per-process in-memory
    cache — politeness still applies, it just forgets across restarts. A
    missing cache must never take a tool down. Tests replace the instance
    through :func:`set_politeness`.
    """
    global _politeness
    if _politeness is None:
        with _politeness_lock:
            if _politeness is None:
                kwargs: dict = dict(
                    timeouts=(5.0, 10.0, TIMEOUT),
                    allowed_hosts=ALLOWED_HOSTS,
                )
                # Same wall-clock budget as the old ``TIMEOUT``: connect 5 s,
                # read 10 s, total 15 s (A3 §5.5 — httpx 0.28 has no
                # ``send(timeout=…)``, the layer applies this per request).
                try:
                    _politeness = Politeness(USER_AGENT, cache_path=default_robots_db_path(), **kwargs)
                except Exception:
                    _politeness = Politeness(USER_AGENT, cache_path=None, **kwargs)
    return _politeness


def set_politeness(politeness: Politeness | None) -> None:
    """Replace (or with ``None`` reset) the process-wide layer. Test seam."""
    global _politeness
    with _politeness_lock:
        _politeness = politeness


def _doc_cache() -> DocCache | None:
    """DocCache for revalidation data, or ``None`` if it cannot be opened.

    A cache problem must never turn into a fetch failure.
    """
    try:
        return DocCache()
    except Exception:
        return None


def _revalidation_for(url: str) -> tuple[dict | None, bytes | None]:
    """``(validators, cached_body)`` stored for ``url`` by an earlier fetch.

    Both are returned together or not at all: a conditional GET without a body
    to serve would throw away the ``304`` (A3 §5.4).
    """
    cache = _doc_cache()
    if cache is None:
        return None, None
    try:
        entry = cache.get_entry(url, include_expired=True)
    except Exception:
        return None, None
    if not entry or not entry.get("body"):
        return None, None
    validators = {
        k: v
        for k, v in (("etag", entry.get("etag")), ("last_modified", entry.get("last_modified")))
        if v
    }
    if not validators:
        return None, None
    return validators, str(entry["body"]).encode("utf-8", "replace")


def _remember_revalidation(url: str, validators: dict, body: str) -> None:
    """Persist validators + raw body so the next fetch can send a conditional GET."""
    if not validators:
        return
    cache = _doc_cache()
    if cache is None:
        return
    try:
        cache.set_validators(
            url,
            etag=validators.get("etag"),
            last_modified=validators.get("last_modified"),
            body=body,
            ttl_seconds=REVALIDATION_TTL_SECONDS,
        )
    except Exception:
        pass  # never break a successful fetch over cache bookkeeping


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _client() -> httpx.Client:
    """The one place an ``httpx.Client`` is built in this module."""
    return httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


def _call_budget(url: str) -> str:
    """Name of a request budget freshly reset for one ``fetch_*`` call.

    The scope is per host and reset at the start of every public call, so it
    caps the fan-out of a single lookup without starving later ones.
    """
    host = (urlparse(url).netloc or "").lower()
    scope = f"fetch:{host}"
    try:
        get_politeness().reset_budget(scope)
    except Exception:
        pass
    return scope


def _http_get(
    client: httpx.Client,
    url: str,
    *,
    validators: dict | None = None,
    cached_body: bytes | None = None,
    budget_scope: str | None = None,
    budget_limit: int = FETCH_BUDGET_LIMIT,
    revalidate: bool = True,
) -> tuple[str | None, str | None, str | None, dict]:
    """GET ``url`` through the politeness layer.

    Returns ``(text, final_url, error, meta)`` — exactly one of text/error is
    set. ``meta`` carries ``status_code`` / ``from_cache`` / ``validators`` /
    ``blocked_by_robots`` for callers that want them. Never raises: the layer
    reports transport failures, ``429`` and robots blocks as ``error``.

    Unless ``revalidate`` is false, the validators and raw body of an earlier
    response for the same URL are read from :class:`DocCache` and offered as
    ``If-None-Match`` / ``If-Modified-Since``, and a fresh ``200`` writes them
    back. Both are passed together or not at all — a conditional GET without a
    body to serve would throw the ``304`` away (A3 §5.4).

    ``status_code`` is truthful — a revalidated response is ``304`` with
    ``from_cache=True`` and the cached body in ``text``, so callers must test
    ``error``/``from_cache`` and not ``status_code == 200`` alone (A3 §5.1).
    """
    if revalidate and validators is None and cached_body is None:
        validators, cached_body = _revalidation_for(url)

    response = get_politeness().get(
        client,
        url,
        validators=validators,
        cached_body=cached_body,
        budget_scope=budget_scope,
        budget_limit=budget_limit,
    )
    meta = {
        "status_code": response.status_code,
        "from_cache": response.from_cache,
        "blocked_by_robots": response.blocked_by_robots,
        "validators": response.validators or {},
    }
    final_url = response.url or url

    if response.blocked_by_robots or response.error:
        return None, final_url, (response.error or f"request to {url} failed"), meta
    if response.from_cache:
        # 304: the body is the one we already had, nothing was re-downloaded.
        # The response carries *fresh* validators, so write them back — keeping
        # the ones we sent would offer a stale ETag on the next revalidation.
        if revalidate:
            _remember_revalidation(url, response.validators or {}, response.text)
        return response.text, final_url, None, meta
    if response.status_code == 404:
        return None, final_url, f"not found (HTTP 404): {url}", meta
    if response.status_code is None or response.status_code >= 400:
        return None, final_url, f"HTTP {response.status_code} from {url}", meta

    if revalidate:
        _remember_revalidation(url, response.validators or {}, response.text)
    return response.text, final_url, None, meta


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


def parse_pub_api_versions(data: dict) -> dict:
    """Extract the published release list from a ``GET /api/packages/{name}`` payload.

    Returns ``{"name", "latest", "versions"}`` where ``versions`` is the list of
    **published** version strings in pub.dev's own order (oldest first), e.g.
    ``[..., "6.1.4", "6.1.5", "6.1.5+1"]``. This is what a version-constrained
    mention is resolved against, so the answer is always a release that really
    exists on pub.dev rather than a guess. Never raises.
    """
    result: dict = {"name": None, "latest": None, "versions": []}
    try:
        if not isinstance(data, dict):
            return result
        result["name"] = data.get("name")
        latest = data.get("latest")
        if isinstance(latest, dict) and isinstance(latest.get("version"), str):
            result["latest"] = latest["version"]
        versions = data.get("versions")
        if isinstance(versions, list):
            result["versions"] = [
                v["version"] for v in versions if isinstance(v, dict) and isinstance(v.get("version"), str)
            ]
        if result["latest"] is None and result["versions"]:
            result["latest"] = result["versions"][-1]
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
    ``{"ok": False, "error"}`` on 404 / network error / timeout / robots block.
    Never raises.
    """
    url = f"{_FLUTTER_API_BASE}/{library}/{class_name}-class.html"
    try:
        with _client() as client:
            text, final_url, error, _meta = _http_get(client, url, budget_scope=_call_budget(url))
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
        with _client() as client:
            text, final_url, error, _meta = _http_get(client, url, budget_scope=_call_budget(url))
    except Exception as exc:  # defensive: never raise out of the fetcher
        return {"ok": False, "error": f"unexpected error: {exc.__class__.__name__}: {exc}"}
    if error is not None:
        return {"ok": False, "error": error}
    return parse_flutter_html(text or "", final_url or url)


def fetch_pub_package(package_name: str, version: str | None = None) -> dict:
    """Fetch pub.dev package metadata plus the README as markdown.

    Hits ``https://pub.dev/api/packages/{name}`` (or ``.../versions/{version}``)
    and the human page ``https://pub.dev/packages/{name}`` — or, when a version
    is pinned, ``https://pub.dev/packages/{name}/versions/{version}`` so the
    README that comes back is the one belonging to **that** release. (Fetching
    the unversioned page for a pinned lookup silently returned the latest
    release's README next to a pinned version number.)

    Returns on success::

        {"ok": True, "name", "version", "description", "publisher",
         "likes", "pub_points", "url", "readme_markdown"}

    ``publisher`` / ``likes`` / ``pub_points`` may be None when the API
    response lacks them (the current API omits them; they are then read from
    the package page when available). If the API call succeeds but the HTML
    page cannot be fetched, the result is still ok with an empty
    ``readme_markdown``. On API failure (404, robots block, budget, network)
    returns ``{"ok": False, "error"}``. Never raises.

    Both pub.dev URLs share one request budget, so a single lookup can never
    make more than :data:`FETCH_BUDGET_LIMIT` requests to the host.
    """
    api_url = f"{_PUB_API_BASE}/{package_name}"
    page_url = f"{_PUB_PAGE_BASE}/{package_name}"
    if version:
        api_url = f"{api_url}/versions/{version}"
        page_url = f"{page_url}/versions/{version}"
    budget_scope = _call_budget(api_url)

    try:
        with _client() as client:
            text, final_url, error, _meta = _http_get(
                client, api_url, budget_scope=budget_scope
            )
            if error is not None:
                return {"ok": False, "error": error}
            try:
                data = json.loads(text or "{}")
            except ValueError as exc:
                return {"ok": False, "error": f"invalid JSON from {api_url}: {exc}"}

            # Best effort: fetch the human page for README + score fallbacks.
            readme_md = ""
            page_meta: dict = {"publisher": None, "likes": None, "pub_points": None}
            page_text, _page_final, page_error, _page_meta = _http_get(
                client, page_url, budget_scope=budget_scope
            )
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


def fetch_pub_versions(package_name: str) -> dict:
    """Fetch the list of **published** versions of a pub.dev package.

    One request to ``https://pub.dev/api/packages/{name}`` — no package page,
    no README — because a version-constrained mention only needs the release
    list to pick from.

    Returns ``{"ok": True, "name", "latest", "versions": [...]}`` or
    ``{"ok": False, "error"}`` (404 / robots block / budget / network).
    Never raises.
    """
    api_url = f"{_PUB_API_BASE}/{package_name}"
    try:
        with _client() as client:
            text, _final_url, error, _meta = _http_get(
                client, api_url, budget_scope=_call_budget(api_url)
            )
    except Exception as exc:  # defensive: never raise out of the fetcher
        return {"ok": False, "error": f"unexpected error: {exc.__class__.__name__}: {exc}"}
    if error is not None:
        return {"ok": False, "error": error}
    try:
        data = json.loads(text or "{}")
    except ValueError as exc:
        return {"ok": False, "error": f"invalid JSON from {api_url}: {exc}"}
    parsed = parse_pub_api_versions(data)
    if not parsed.get("versions") and not parsed.get("latest"):
        return {"ok": False, "error": f"pub.dev returned no version list for '{package_name}'"}
    parsed["ok"] = True
    parsed["url"] = api_url
    return parsed
