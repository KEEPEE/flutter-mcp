"""Local search index over Flutter and Dart SDK classes.

The index is a JSON-serializable dict::

    {"built_at": "<iso8601 utc>", "entries": [{"name", "library", "kind", "url"}, ...]}

Sources (verified against the live sites — note that
``https://api.flutter.dev/flutter/classes-index.html`` does NOT exist / 404s):

- Flutter: dartdoc generates one *library overview* page per library at
  ``https://api.flutter.dev/flutter/{lib}/`` whose main content holds
  ``<h2>Section</h2><dl><dt><span class="name"><a href=...>Name</a></span></dt>``
  member lists. The library list is discovered from the sidebar of
  ``https://api.flutter.dev/index.html``.
- Dart SDK: same dartdoc layout on ``https://api.dart.dev/{lib}/`` with the
  library list taken from ``https://api.dart.dev/index.html`` (``dart-*``
  links).

Member links on a library page are relative to that page's directory and
point at the member's *canonical* library segment, e.g. on the ``material``
page: ``widgets/ListView-class.html`` (which resolves to a nested view under
``/flutter/material/``). We therefore normalize every entry URL to the
canonical form ``{site_base}/{lib}/{filename}`` — for classes this is exactly
the shape produced by :func:`flutter_docs_mcp.fetchers.fetch_flutter_class_doc`
/ :func:`fetch_dart_class_doc`.

Verified link shapes (current dartdoc builds, checked against both live
sites): the ``Classes`` section links use ``{Name}-class.html``, ``Mixins``
uses ``{Name}-mixin.html``, but ``Enums`` and ``Typedefs`` use *plain*
``{Name}.html`` URLs. The entry ``kind`` is therefore taken from the section
heading when it is one of Classes/Enums/Mixins/Typedefs, and otherwise from
the link suffix (``-class.html``, ``-enum.html``, ``-mixin.html``,
``-typedef.html``); links with neither signal are skipped (functions,
constants, extensions, extension types).

Only :func:`build_index` (and via it :func:`load_index`) touch the network;
everything else in this module is importable and usable offline.

Politeness: a cold index build is ~47 sequential requests (1 + 25 library
pages on api.flutter.dev, 1 + 20 on api.dart.dev), so every page goes
through :mod:`flutter_docs_mcp.politeness` (robots.txt, per-host throttle) and
consumes a per-host request budget of :data:`INDEX_BUDGET_LIMIT`. When the
budget runs out the build stops early and returns the **partial** index it has
(``"partial": True``) instead of raising; :func:`load_index` then refuses to
cache that truncated index, and ``Politeness.stats()["budget_denied"]`` shows
what happened.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .cache import DocCache
from .fetchers import get_politeness

__all__ = ["build_index", "load_index", "search", "parse_library_page"]

USER_AGENT = "flutter-docs-mcp/0.2 (+https://github.com/KEEPEE/flutter-mcp)"
TIMEOUT = 20.0

#: Layer calls one index build may make per host (A2 §4.3 measured a cold build
#: at 33 requests on api.flutter.dev).  Re-measured live after the A8 port (A9
#: smoke, cold build): **24** on api.flutter.dev (index page + 23 library pages)
#: and **7** on api.dart.dev, so 40 still keeps ~65% headroom and is left
#: alone — the A8 growth does not come out of this counter: ``build_index``
#: charges one unit per *layer call* and the layer does robots.txt and any
#: redirect hop inside that call.  Still low enough that a runaway loop cannot
#: hammer the doc sites.
INDEX_BUDGET_LIMIT = 40

_FLUTTER_INDEX_PAGE = "https://api.flutter.dev/index.html"
_FLUTTER_BASE = "https://api.flutter.dev/flutter"
_DART_INDEX_PAGE = "https://api.dart.dev/index.html"
_DART_BASE = "https://api.dart.dev"

INDEX_CACHE_KEY = "search-index"
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600  # one week

# Fallbacks used only when the index landing pages cannot be fetched. These are
# the library sets the live sites publish today (verified against
# api.flutter.dev/index.html and api.dart.dev/index.html), so a fallback build
# is current rather than a stale guess.
_FALLBACK_FLUTTER_LIBS = [
    "animation", "cupertino", "flutter_driver", "flutter_driver_extension",
    "flutter_gpu", "flutter_localizations", "flutter_test",
    "flutter_web_plugins", "foundation", "gestures", "material", "meta",
    "meta_dart2js", "meta_meta", "painting", "physics", "rendering",
    "scheduler", "semantics", "services", "vm_service", "widget_previews",
    "widgets", "dart-ui", "dart-ui_web",
]
_FALLBACK_DART_LIBS = [
    "dart-async", "dart-collection", "dart-convert", "dart-core",
    "dart-developer", "dart-ffi", "dart-html", "dart-indexed_db", "dart-io",
    "dart-isolate", "dart-js", "dart-js_interop", "dart-js_interop_unsafe",
    "dart-js_util", "dart-math", "dart-mirrors", "dart-svg",
    "dart-typed_data", "dart-web_audio", "dart-web_gl",
]

#: Libraries that exist **only** on api.flutter.dev under a ``dart-`` name: the
#: engine's own UI libraries (``dart:ui`` as Flutter ships it). They must survive
#: the filter that drops ``dart-*`` / ``package-*`` links on the Flutter index
#: page — those point at the other site or at third-party docs.
_FLUTTER_ONLY_DART_LIBS = frozenset({"dart-ui", "dart-ui_web"})

# "{canonical-lib}/{file}.html" (links pointing at a *different* library's page)
_PREFIXED_RE = re.compile(r"^([A-Za-z0-9_-]+)/([A-Za-z0-9_<>][A-Za-z0-9_<>.-]*\.html)$")
# "{file}.html" (links relative to the current library page)
_BARE_RE = re.compile(r"^([A-Za-z0-9_<>][A-Za-z0-9_<>.-]*\.html)$")
_SUFFIX_KIND_RE = re.compile(r"-(class|enum|mixin|typedef)\.html$")
# Library directory on the Flutter index. Hyphens are allowed because Flutter
# publishes two libraries with them (``dart-ui/``, ``dart-ui_web/``); every other
# lowercase hyphenated directory on that page is a ``dart-*`` cross-site link or a
# ``package-*`` third-party link, and :func:`_discover_libs` drops those.
_LIB_LINK_RE = re.compile(r"^[a-z][a-z0-9_-]*/$")
_DART_LIB_LINK_RE = re.compile(r"^dart-[a-z_]+/$")

# Section headings (lowercased) that unambiguously identify the member kind.
_SECTION_KIND = {
    "classes": "class",
    "enums": "enum",
    "mixins": "mixin",
    "typedefs": "typedef",
}

# Pure-fuzzy matches (no exact/startswith/substring relation) below this
# SequenceMatcher ratio are treated as noise and dropped.
_MIN_FUZZY_RATIO = 0.3


# ---------------------------------------------------------------------------
# Network layer (single monkeypatch point for offline tests)
# ---------------------------------------------------------------------------

def _client() -> httpx.Client:
    """The one place an ``httpx.Client`` is built here (test seam)."""
    return httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


# Scopes whose request budget ran out during the *current* build. Kept at
# module level so build_index can stop looping instead of probing every
# remaining library page and burning a denial each time.
_exhausted_lock = threading.Lock()
_exhausted_scopes: set[str] = set()


def _budget_scope(url: str) -> str:
    """Per-host budget scope of an index URL, e.g. ``index:api.dart.dev``."""
    return f"index:{(urlparse(url).netloc or '').lower()}"


def _reset_index_budgets() -> None:
    """Give every host a fresh :data:`INDEX_BUDGET_LIMIT` for this build."""
    with _exhausted_lock:
        _exhausted_scopes.clear()
    try:
        pol = get_politeness()
    except Exception:
        return  # a broken budget layer must not stop a build
    for url in (_FLUTTER_INDEX_PAGE, _DART_INDEX_PAGE):
        try:
            pol.reset_budget(_budget_scope(url))
        except Exception:
            pass  # a broken budget layer must not stop a build


def _take_index_budget(url: str) -> bool:
    """Consume one unit of the host's index budget (``False`` when exhausted)."""
    scope = _budget_scope(url)
    try:
        allowed = get_politeness().budget(scope, INDEX_BUDGET_LIMIT)
    except Exception:
        return True
    if not allowed:
        with _exhausted_lock:
            _exhausted_scopes.add(scope)
    return allowed


def _budget_exhausted_for(url: str) -> bool:
    with _exhausted_lock:
        return _budget_scope(url) in _exhausted_scopes


def _exhausted_scopes_snapshot() -> list[str]:
    with _exhausted_lock:
        return sorted(_exhausted_scopes)


def _http_get_text(url: str) -> str | None:
    """GET ``url`` through the politeness layer; body text or ``None`` on failure.

    Failures include robots blocks, an exhausted index budget, ``4xx``/``5xx``
    and transport errors — all of them are simply "no page here" for the index,
    which keeps partial builds and stale fallbacks working. Never raises.
    """
    if not _take_index_budget(url):
        return None
    try:
        with _client() as client:
            response = get_politeness().get(client, url)
    except Exception:
        return None
    if not response.ok:
        return None
    return response.text


# ---------------------------------------------------------------------------
# Pure parsing (no network)
# ---------------------------------------------------------------------------

def _strip_relative_prefix(href: str) -> str:
    h = href.strip()
    while True:
        if h.startswith("./"):
            h = h[2:]
        elif h.startswith("../"):
            h = h[3:]
        else:
            break
    return h.split("#", 1)[0].split("?", 1)[0]


def _page_library(page_url: str) -> str:
    """Library segment of a library-landing page URL ("" when not one)."""
    path = urlparse(page_url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def _site_base(page_url: str) -> str:
    if page_url.startswith(_FLUTTER_BASE):
        return _FLUTTER_BASE
    if page_url.startswith(_DART_BASE):
        return _DART_BASE
    parts = urlparse(page_url)
    return f"{parts.scheme}://{parts.netloc}{urlparse(page_url).path.rstrip('/')}"


def parse_library_page(html: str, page_url: str) -> list[dict]:
    """Parse a dartdoc library-overview page into index entries.

    Scans the main content for ``<h2>/<h3>`` sections followed by a ``<dl>``,
    and reads the member-name link from each ``<dt>``. The entry ``kind`` is
    taken from the section heading when it is one of Classes/Enums/Mixins/
    Typedefs (current dartdoc uses plain ``{Name}.html`` URLs for enums and
    typedefs), otherwise from the link suffix (``-class.html``,
    ``-enum.html``, ``-mixin.html``, ``-typedef.html``, default ``"class"``);
    links with neither signal are skipped. URLs are normalized to the
    canonical absolute form ``{site_base}/{lib}/{filename}``.

    Pure function — never raises, returns [] when the page shape is unknown.
    """
    entries: list[dict] = []
    try:
        soup = BeautifulSoup(html, "lxml")
        main = (
            soup.select_one("#dartdoc-main-content")
            or soup.select_one("div.main-content")
            or soup.find("main")
        )
        if main is None:
            return []

        base = _site_base(page_url)
        own_lib = _page_library(page_url)

        for heading in main.find_all(["h2", "h3"]):
            section_kind = _SECTION_KIND.get(heading.get_text(" ", strip=True).lower())
            sibling = heading.find_next_sibling()
            for _ in range(3):  # tolerate stray nodes between heading and list
                if sibling is not None and sibling.name == "dl":
                    break
                if sibling is None:
                    break
                sibling = sibling.find_next_sibling()
            if sibling is None or sibling.name != "dl":
                continue

            for dt in sibling.find_all("dt"):
                a = dt.select_one("span.name > a") or dt.find("a", href=True)
                if a is None or not a.get("href"):
                    continue
                href = _strip_relative_prefix(a["href"])
                m = _PREFIXED_RE.match(href) or _BARE_RE.match(href)
                if m is None:
                    continue
                lib, filename = (
                    (m.group(1), m.group(2)) if m.lastindex == 2 else (own_lib, m.group(1))
                )
                kind = section_kind
                if kind is None:
                    suffix = _SUFFIX_KIND_RE.search(filename)
                    if suffix is None:
                        continue  # function / constant / extension — not indexable here
                    kind = suffix.group(1)
                name_from_url = filename.rsplit(".", 1)[0]
                for sfx in ("-class", "-enum", "-mixin", "-typedef"):
                    if name_from_url.endswith(sfx):
                        name_from_url = name_from_url[: -len(sfx)]
                        break
                entries.append({
                    "name": a.get_text(strip=True) or re.sub(r"[<>]", "", name_from_url),
                    "library": lib,
                    "kind": kind,
                    "url": f"{base}/{lib}/{filename}",
                })
    except Exception:  # defensive: parser must never raise
        return []
    return entries


def _discover_libs(index_html: str | None, pattern: re.Pattern, fallback: list[str],
                   *, keep_dart_prefixed: bool = False) -> list[str]:
    """Extract library names (without trailing slash) from an index landing page.

    ``keep_dart_prefixed`` decides what a ``dart-*`` link means:

    - **api.dart.dev** names its libraries ``dart-core``, ``dart-async``, …, so
      those names must be kept. Dropping them made Dart discovery return an
      empty set on every build, which silently fell back to a short hardcoded
      list and left ``dart:convert`` / ``dart:isolate`` / ``dart:ffi`` /
      ``dart:js_interop`` / ``dart:typed_data`` … out of the index entirely.
    - **api.flutter.dev** links ``dart-*`` only to point at the *other* site,
      and ``package-*`` links point at third-party docs; both are dropped there
      — except Flutter's own ``dart-ui`` / ``dart-ui_web`` libraries
      (:data:`_FLUTTER_ONLY_DART_LIBS`).

    Platform directories (``Android``, ``iOS``, …) never match the lowercase
    patterns in the first place.
    """
    if not index_html:
        return list(fallback)
    try:
        soup = BeautifulSoup(index_html, "lxml")
        found = {
            a["href"][:-1]
            for a in soup.find_all("a", href=True)
            if pattern.fullmatch(a["href"])
        }
        if not keep_dart_prefixed:
            found = {
                name for name in found
                if not name.startswith("package-")
                and (not name.startswith("dart-") or name in _FLUTTER_ONLY_DART_LIBS)
            }
        return sorted(found) or list(fallback)
    except Exception:
        return list(fallback)


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def build_index() -> dict:
    """Fetch the class lists from api.flutter.dev and api.dart.dev.

    Returns ``{"built_at": iso, "entries": [...]}``. Raises
    :class:`RuntimeError` when no entries could be fetched at all (e.g. the
    network is down); partial failures of individual library pages are
    skipped silently.

    When a host's request budget runs out the loop stops early and the result
    is a **partial** index — ``{"partial": True, "partial_reason": …}`` — not
    an exception. That is deliberate: a truncated index still answers
    ``flutter_search``, and :func:`load_index` will not cache it.
    """
    entries_by_url: dict[str, dict] = {}
    _reset_index_budgets()

    flutter_libs = _discover_libs(_http_get_text(_FLUTTER_INDEX_PAGE), _LIB_LINK_RE, _FALLBACK_FLUTTER_LIBS)
    for lib in flutter_libs:
        page_url = f"{_FLUTTER_BASE}/{lib}/"
        if _budget_exhausted_for(page_url):
            break  # budget spent for this host — keep whatever we have
        html = _http_get_text(page_url)
        if not html:
            continue
        for entry in parse_library_page(html, page_url):
            entries_by_url.setdefault(entry["url"], entry)

    dart_libs = _discover_libs(
        _http_get_text(_DART_INDEX_PAGE), _DART_LIB_LINK_RE, _FALLBACK_DART_LIBS,
        keep_dart_prefixed=True,
    )
    for lib in dart_libs:
        page_url = f"{_DART_BASE}/{lib}/"
        if _budget_exhausted_for(page_url):
            break
        html = _http_get_text(page_url)
        if not html:
            continue
        for entry in parse_library_page(html, page_url):
            entries_by_url.setdefault(entry["url"], entry)

    if not entries_by_url:
        raise RuntimeError(
            "could not build search index: no library pages could be fetched "
            f"(tried {len(flutter_libs)} Flutter + {len(dart_libs)} Dart libraries)"
        )

    entries = sorted(entries_by_url.values(), key=lambda e: (e["name"].lower(), e["url"]))
    index = {"built_at": datetime.now(timezone.utc).isoformat(), "entries": entries}

    exhausted = _exhausted_scopes_snapshot()
    if exhausted:
        index["partial"] = True
        index["partial_reason"] = (
            f"request budget of {INDEX_BUDGET_LIMIT} per host exhausted for "
            f"{', '.join(exhausted)}; the index is incomplete"
        )
    return index


def load_index(force_refresh: bool = False, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> dict:
    """Return the search index, using a :class:`DocCache` to avoid refetching.

    - Fresh cached copy (age < ``max_age_seconds``) → returned as-is.
    - Missing/expired/``force_refresh`` → rebuild; on success the new index
      is cached with TTL ``max_age_seconds``.
    - Rebuild failure + an old cached copy exists → the old copy is returned
      with an added ``"stale": True`` flag. Never raises.
    - The cache itself may be **absent** (A13 B3 / A12 F-A12-2): an unwritable
      or read-only cache directory makes :class:`DocCache` raise, and that must
      not take the index — and with it every tool — down.  Same contract as
      java-spring-mcp / python-docs-mcp / js-ts-mcp: degrade to build-only.
    """
    cache: DocCache | None
    try:
        cache = DocCache()
    except Exception:
        cache = None  # cache unavailable; degrade to build-only behaviour

    if not force_refresh and cache is not None:
        try:
            cached = cache.get(INDEX_CACHE_KEY)
        except Exception:
            cached = None
        if cached is not None:
            try:
                return json.loads(cached)
            except ValueError:
                pass  # corrupt payload — fall through to a rebuild

    try:
        index = build_index()
    except Exception as exc:
        old = cache.peek(INDEX_CACHE_KEY) if cache is not None else None
        if old is not None:
            try:
                data = json.loads(old)
                data["stale"] = True
                return data
            except ValueError:
                pass
        return {
            "built_at": None,
            "entries": [],
            "stale": True,
            "error": f"index build failed and no cached copy is available: {exc}",
        }

    if index.get("partial"):
        # A budget-truncated index is served once but never stored: caching it
        # for a week would freeze a knowingly incomplete class list.
        return index

    if cache is not None:
        try:
            cache.set(INDEX_CACHE_KEY, json.dumps(index), max_age_seconds)
        except Exception:  # a cache-write failure must not break the response
            pass
    return index


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _score_entry(query: str, name: str) -> float | None:
    """Score one entry name against the (lowercased) query.

    Tiered so ordering is always exact > startswith > substring > fuzzy:
    ``score = tier_weight + SequenceMatcher.ratio`` with weights
    3 / 2 / 1 / 0. Pure-fuzzy matches below :data:`_MIN_FUZZY_RATIO` are
    dropped (returns ``None``).
    """
    n = name.lower()
    ratio = SequenceMatcher(None, query, n).ratio()
    if n == query:
        return 3.0 + ratio
    if n.startswith(query):
        return 2.0 + ratio
    if query in n:
        return 1.0 + ratio
    if ratio < _MIN_FUZZY_RATIO:
        return None
    return ratio


def search(query: str | None, limit: int = 8, index: dict | None = None) -> list[dict]:
    """Rank index entries by name similarity to ``query`` (case-insensitive).

    Returns up to ``limit`` entries, each a copy of the index entry augmented
    with a ``"score"`` key, best first. Empty/None query → ``[]``.

    ``index`` may be passed explicitly (hand-built or pre-loaded) to avoid
    any cache/network access; by default :func:`load_index` is used, which
    itself never raises.
    """
    if not query or not str(query).strip():
        return []
    q = str(query).strip().lower()

    if index is None:
        index = load_index()
    entries = index.get("entries") if isinstance(index, dict) else None
    if not entries:
        return []

    scored: list[tuple[float, int, dict]] = []
    for i, entry in enumerate(entries):
        name = entry.get("name") or ""
        score = _score_entry(q, name)
        if score is None:
            continue
        scored.append((score, i, entry))

    # Sort by score desc; ties broken by original index order (stable).
    scored.sort(key=lambda item: (-item[0], item[1]))
    results = []
    for score, _i, entry in scored[: max(0, int(limit))]:
        out = dict(entry)
        out["score"] = round(score, 4)
        results.append(out)
    return results
