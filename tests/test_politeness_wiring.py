"""Wiring tests: do the repo's HTTP paths really go through the politeness layer?

The unit tests in :mod:`tests.test_politeness` prove the layer itself is
correct. These tests prove the *integration* — that ``fetch_*``, the index
build and ``flutter_status`` cannot bypass it. Everything is offline: the only
HTTP client in play is ``httpx.MockTransport``.

Seams used (all module-level on purpose):

- ``fetchers._client``            → MockTransport client
- ``fetchers.set_politeness``     → a layer with an injected clock / RNG
- ``search._client``              → MockTransport client for the index build
- ``search.INDEX_BUDGET_LIMIT``   → a budget small enough to exhaust
"""

from __future__ import annotations

import random

import httpx
import pytest

import flutter_docs_mcp.fetchers as fetchers_mod
import flutter_docs_mcp.search as search_mod
import flutter_docs_mcp.server as server_mod
from flutter_docs_mcp.cache import DocCache
from flutter_docs_mcp.fetchers import fetch_dart_class_doc, fetch_flutter_class_doc, fetch_pub_package
from flutter_docs_mcp.politeness import Politeness
from flutter_docs_mcp.server import flutter_status

FLUTTER_PAGE_URL = "https://api.flutter.dev/flutter/widgets/ListView-class.html"
DART_PAGE_URL = "https://api.dart.dev/dart-async/Future-class.html"

WELCOME_ROBOTS = b"# All robots welcome!\n"

PAGE_HTML = """<html><head><title>ListView</title></head><body>
<div id="dartdoc-main-content">
<h1>ListView class</h1>
<p>A scrollable list of widgets arranged linearly.</p>
<h2>Constructors</h2><p>ListView()</p>
</div></body></html>"""

DART_HTML = """<html><body><div id="dartdoc-main-content">
<h1>Future class</h1><p>A value available later.</p>
</div></body></html>"""

PUB_API_JSON = b'{"name":"dio","latest":{"version":"5.4.0","pubspec":{"name":"dio","description":"HTTP"}}}'
PUB_PAGE_HTML = b"""<html><body><section class="detail-tab-readme-content">
<h1>dio</h1><p>HTTP networking package</p></section>
<a class="-pub-publisher" href="#">flutter.cn</a></body></html>"""


class FakeClock:
    """Monotonic clock + a sleep that advances it (no real waiting in tests)."""

    def __init__(self, start: float = 1_000.0, wall: float = 1_700_000_000.0) -> None:
        self.t = start
        self.wall = wall
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def wall_now(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class Recorder:
    """MockTransport handler: canned routes + a record of every request."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        action = self.routes.get(path, self.routes.get("*"))
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request, self)
        if isinstance(action, int):
            return httpx.Response(action, content=b"nope")
        headers = {}
        if path != "/robots.txt":
            headers = {"etag": '"v1"', "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT"}
        return httpx.Response(200, content=action or b"", headers=headers)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def hits(self, path: str) -> int:
        return self.paths().count(path)

    def for_path(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def use_transport(monkeypatch, module, recorder: Recorder) -> Recorder:
    """Route every client ``module`` builds through ``recorder``."""
    monkeypatch.setattr(module, "_client", lambda: httpx.Client(transport=httpx.MockTransport(recorder)))
    return recorder


def fast_layer(**kw) -> Politeness:
    """A politeness layer with no throttling sleeps (real clock unless given one)."""
    kw.setdefault("base_delay", (0.0, 0.0))
    kw.setdefault("rng", random.Random(1234))
    return Politeness(fetchers_mod.USER_AGENT, cache_path=kw.pop("cache_path", None), **kw)


# ---------------------------------------------------------------------------
# 1. fetch_* really goes through the layer
# ---------------------------------------------------------------------------

def test_fetch_flutter_class_doc_passes_through_the_layer(monkeypatch):
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": PAGE_HTML.encode(),
    }))

    result = fetch_flutter_class_doc("ListView", "widgets")

    assert result["ok"] is True
    assert "A scrollable list of widgets arranged linearly." in result["markdown"]
    # robots.txt was consulted before the page — that is the whole point.
    assert rec.hits("/robots.txt") == 1
    assert rec.hits("/flutter/widgets/ListView-class.html") == 1
    assert rec.paths().index("/robots.txt") < rec.paths().index("/flutter/widgets/ListView-class.html")
    # The layer sets the UA per request, so the polite UA is on the wire.
    assert rec.requests[-1].headers["user-agent"] == fetchers_mod.USER_AGENT
    stats = fetchers_mod.get_politeness().stats()
    assert stats["requests"] >= 1 and stats["robots_fetches"] == 1


def test_robots_disallow_returns_ok_false_and_never_fetches_the_page(monkeypatch):
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": b"User-agent: *\nDisallow: /flutter/widgets/\n",
        "/flutter/widgets/ListView-class.html": PAGE_HTML.encode(),
    }))

    result = fetch_flutter_class_doc("ListView", "widgets")

    assert result["ok"] is False
    assert "blocked by robots.txt" in result["error"]
    assert "Disallow: /flutter/widgets/" in result["error"]
    assert rec.hits("/flutter/widgets/ListView-class.html") == 0  # fail-closed
    assert fetchers_mod.get_politeness().stats()["blocked_by_robots"] == 1


def test_robots_txt_is_fetched_once_per_host_across_calls(monkeypatch):
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": PAGE_HTML.encode(),
        "/flutter/material/AppBar-class.html": PAGE_HTML.encode(),
        "/dart-async/Future-class.html": DART_HTML.encode(),
    }))

    assert fetch_flutter_class_doc("ListView")["ok"] is True
    assert fetch_flutter_class_doc("AppBar", "material")["ok"] is True
    assert fetch_dart_class_doc("Future", "dart:async")["ok"] is True

    assert rec.hits("/flutter/widgets/ListView-class.html") == 1
    assert rec.hits("/dart-async/Future-class.html") == 1
    # 2 hosts → exactly 2 robots requests for 3 page fetches.
    assert rec.hits("/robots.txt") == 2
    stats = fetchers_mod.get_politeness().stats()
    # 2 fetches (one per host) + 1 cache hit (the second Flutter page).
    assert stats["robots_fetches"] == 2 and stats["robots_cache_hits"] == 1


def test_robots_cache_is_a_file_shared_across_layer_instances(monkeypatch, tmp_path):
    """Production points the robots cache at ``robots.db``: a restart re-uses it.

    The autouse fixture uses an in-memory cache for speed, so the file-backed
    path — the one that makes a server restart cheap — is asserted here.
    """
    db = tmp_path / "robots.db"
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": PAGE_HTML.encode(),
        "/flutter/material/AppBar-class.html": PAGE_HTML.encode(),
    }))

    fetchers_mod.set_politeness(fast_layer(cache_path=str(db)))
    assert fetch_flutter_class_doc("ListView")["ok"] is True
    assert rec.hits("/robots.txt") == 1
    assert db.exists()

    # A brand-new layer over the same file == a server restart.
    fetchers_mod.set_politeness(fast_layer(cache_path=str(db)))
    assert fetch_flutter_class_doc("AppBar", "material")["ok"] is True
    assert rec.hits("/robots.txt") == 1  # not re-fetched
    assert fetchers_mod.get_politeness().stats()["robots_cache_hits"] == 1


def test_transport_error_is_still_retried_once(monkeypatch):
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": httpx.ConnectError("connection refused"),
    }))

    result = fetch_flutter_class_doc("ListView")

    assert result["ok"] is False
    assert "transport error" in result["error"]
    # The old hand-rolled loop made 2 attempts; the layer must not change that.
    assert rec.hits("/flutter/widgets/ListView-class.html") == 2
    assert fetchers_mod.get_politeness().stats()["retries_transport"] == 2


def test_429_with_retry_after_is_honoured_and_retried_once(monkeypatch):
    clock = FakeClock()
    fetchers_mod.set_politeness(fast_layer(clock=clock.now, sleep=clock.sleep, wall_clock=clock.wall_now))

    def page(request: httpx.Request, rec: Recorder) -> httpx.Response:
        if len(rec.for_path("/flutter/widgets/ListView-class.html")) == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, content=b"slow down")
        return httpx.Response(200, content=PAGE_HTML.encode(), headers={"etag": '"v1"'})

    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": page,
    }))

    result = fetch_flutter_class_doc("ListView")

    assert result["ok"] is True
    assert 3.0 in clock.slept  # waited exactly what the server asked for
    assert rec.hits("/flutter/widgets/ListView-class.html") == 2  # one retry, no more
    stats = fetchers_mod.get_politeness().stats()
    assert stats["retry_after_honoured"] == 1 and stats["retries_429"] == 1


def test_opt_out_env_var_bypasses_robots_and_throttle(monkeypatch):
    monkeypatch.setenv("FLUTTER_DOCS_MCP_POLITENESS_DISABLED", "1")
    fetchers_mod.set_politeness(fast_layer())
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": b"User-agent: *\nDisallow: /flutter/\n",
        "/flutter/widgets/ListView-class.html": PAGE_HTML.encode(),
    }))

    result = fetch_flutter_class_doc("ListView")

    assert result["ok"] is True  # explicitly opted out, at the user's risk
    assert rec.hits("/robots.txt") == 0
    assert fetchers_mod.get_politeness().stats()["disabled"] is True


# ---------------------------------------------------------------------------
# 2. conditional GET: validators + body survive in DocCache
# ---------------------------------------------------------------------------

def test_fetch_stores_validators_and_body_in_the_sqlite_cache(monkeypatch):
    use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": PAGE_HTML.encode(),
    }))

    assert fetch_flutter_class_doc("ListView")["ok"] is True

    entry = DocCache().get_entry(FLUTTER_PAGE_URL, include_expired=True)
    assert entry is not None
    assert entry["etag"] == '"v1"'
    assert entry["last_modified"] == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert "scrollable list" in entry["body"]


def test_expired_row_is_revalidated_instead_of_downloaded_again(monkeypatch):
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": lambda request, r: (
            httpx.Response(304, headers={"etag": '"v2"'})
            if request.headers.get("if-none-match")
            else httpx.Response(200, content=PAGE_HTML.encode(),
                                headers={"etag": '"v1"',
                                         "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT"})
        ),
    }))

    first = fetch_flutter_class_doc("ListView")
    assert first["ok"] is True

    # A brand-new layer has an empty in-memory body cache: only SQLite can
    # answer, which is exactly the cross-restart case.
    fetchers_mod.set_politeness(fast_layer())
    second = fetch_flutter_class_doc("ListView")

    assert second["ok"] is True
    assert second["markdown"] == first["markdown"]  # body came from the cache
    page_requests = rec.for_path("/flutter/widgets/ListView-class.html")
    assert len(page_requests) == 2
    assert page_requests[0].headers.get("if-none-match") is None
    assert page_requests[1].headers["if-none-match"] == '"v1"'
    assert page_requests[1].headers["if-modified-since"] == "Wed, 21 Oct 2015 07:28:00 GMT"
    stats = fetchers_mod.get_politeness().stats()
    assert stats["revalidated_304"] == 1 and stats["conditional"] == 1
    # The *refreshed* validator must land in the cache, not the one we sent.
    assert DocCache().get_entry(FLUTTER_PAGE_URL, include_expired=True)["etag"] == '"v2"'


def test_conditional_get_is_never_sent_without_a_cached_body(monkeypatch):
    """A 304 with no body to serve would lose the content — must not happen."""
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/flutter/widgets/ListView-class.html": lambda request, r: (
            httpx.Response(304, headers={"etag": '"v2"'})
            if request.headers.get("if-none-match")
            else httpx.Response(200, content=PAGE_HTML.encode(),
                                headers={"etag": '"v1"',
                                         "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT"})
        ),
    }))
    # Validators present, body deliberately missing.
    DocCache().set_validators(FLUTTER_PAGE_URL, etag='"v1"', last_modified=None, body=None)

    result = fetch_flutter_class_doc("ListView")

    assert result["ok"] is True
    assert rec.for_path("/flutter/widgets/ListView-class.html")[0].headers.get("if-none-match") is None
    assert fetchers_mod.get_politeness().stats()["conditional"] == 0


def test_pub_package_fetches_api_and_page_through_the_layer(monkeypatch):
    rec = use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/api/packages/dio": PUB_API_JSON,
        "/packages/dio": PUB_PAGE_HTML,
    }))

    result = fetch_pub_package("dio")

    assert result["ok"] is True
    assert result["name"] == "dio"
    assert "HTTP networking package" in result["readme_markdown"]
    assert rec.hits("/robots.txt") == 1  # one host, one robots fetch for 2 URLs
    assert rec.hits("/api/packages/dio") == 1
    assert rec.hits("/packages/dio") == 1


# ---------------------------------------------------------------------------
# 3. index build: budget cap + throttle, never an exception
# ---------------------------------------------------------------------------

def _lib_page(*names: str) -> str:
    items = "".join(
        f'<dt id="{n}"><span class="name"><a href="{n}-class.html">{n}</a></span></dt>'
        for n in names
    )
    return f'<html><body><div id="dartdoc-main-content"><h2>Classes</h2><dl>{items}</dl></div></body></html>'


FLUTTER_INDEX_HTML = (
    '<html><body><a href="widgets/">widgets</a><a href="material/">material</a>'
    '<a href="cupertino/">cupertino</a><a href="services/">services</a></body></html>'
).encode()
DART_INDEX_HTML = (
    '<html><body><a href="dart-core/">core</a><a href="dart-async/">async</a></body></html>'
).encode()

INDEX_ROUTES: dict[str, object] = {
    "/robots.txt": WELCOME_ROBOTS,
    "/flutter/widgets/": _lib_page("ListView", "ScrollView"),
    "/flutter/material/": _lib_page("AppBar", "Scaffold"),
    "/flutter/cupertino/": _lib_page("CupertinoButton"),
    "/flutter/services/": _lib_page("SystemChannels"),
    "/dart-core/": _lib_page("String", "Object"),
    "/dart-async/": _lib_page("Future", "Stream"),
}


def flutter_index_routes() -> dict[str, object]:
    """Routes for a whole build; ``/index.html`` depends on the host."""

    def index_page(request: httpx.Request, _rec: Recorder) -> httpx.Response:
        body = DART_INDEX_HTML if request.url.host == "api.dart.dev" else FLUTTER_INDEX_HTML
        return httpx.Response(200, content=body)

    routes = dict(INDEX_ROUTES)
    routes["/index.html"] = index_page
    return routes


def test_index_build_budget_cap_yields_a_partial_index(monkeypatch):
    rec = use_transport(monkeypatch, search_mod, Recorder(flutter_index_routes()))
    monkeypatch.setattr(search_mod, "INDEX_BUDGET_LIMIT", 3)

    index = search_mod.build_index()  # must NOT raise

    assert index["entries"], "a capped build must still return what it fetched"
    assert index["partial"] is True
    assert "budget" in index["partial_reason"]
    # 3 requests per host: index page + 2 library pages, then the loop stops.
    # ``_discover_libs`` returns the libraries sorted, so cupertino+material
    # are the two that fit and services/widgets are never requested.
    assert rec.hits("/flutter/cupertino/") == 1
    assert rec.hits("/flutter/material/") == 1
    assert rec.hits("/flutter/services/") == 0
    assert rec.hits("/flutter/widgets/") == 0
    stats = fetchers_mod.get_politeness().stats()
    assert stats["budget_denied"] >= 1
    assert stats["budgets"]["index:api.flutter.dev"] == [3, 3]


def test_uncapped_index_build_is_not_marked_partial(monkeypatch):
    use_transport(monkeypatch, search_mod, Recorder(flutter_index_routes()))

    index = search_mod.build_index()

    assert "partial" not in index
    names = {e["name"] for e in index["entries"]}
    assert {"ListView", "AppBar", "CupertinoButton", "String", "Future"} <= names


def test_partial_index_is_not_cached_for_a_week(monkeypatch):
    use_transport(monkeypatch, search_mod, Recorder(flutter_index_routes()))
    monkeypatch.setattr(search_mod, "INDEX_BUDGET_LIMIT", 2)

    index = search_mod.load_index()

    assert index.get("partial") is True
    assert DocCache().get(search_mod.INDEX_CACHE_KEY) is None  # not persisted
    # …but a full build right after it is, so the cap is not sticky.
    monkeypatch.setattr(search_mod, "INDEX_BUDGET_LIMIT", 40)
    full = search_mod.load_index()
    assert "partial" not in full
    assert DocCache().get(search_mod.INDEX_CACHE_KEY) is not None


def test_index_pages_are_politely_throttled_per_host(monkeypatch):
    """The build is sequential; the layer must space it out (A3 §5.9)."""
    clock = FakeClock()
    fetchers_mod.set_politeness(Politeness(
        fetchers_mod.USER_AGENT, cache_path=None,
        base_delay=(0.5, 0.5), clock=clock.now, sleep=clock.sleep, rng=random.Random(7),
    ))
    use_transport(monkeypatch, search_mod, Recorder(flutter_index_routes()))

    search_mod.build_index()

    stats = fetchers_mod.get_politeness().stats()
    assert stats["throttle_waits"] >= 1
    assert stats["throttle_sleep_s"] > 0
    assert clock.slept and max(clock.slept) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 4. flutter_status exposes the counters without changing its checks
# ---------------------------------------------------------------------------

class _StatusResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = "ok"
        self.content = b"ok"
        self.headers: dict[str, str] = {}
        self.url = "https://api.flutter.dev/"


class _StatusClient:
    """Minimal httpx.Client stand-in for the politeness-aware status probes."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get(self, url: str) -> _StatusResponse:
        return _StatusResponse(200)

    def build_request(self, method, url, headers=None, timeout=None):
        return httpx.Request(method, url, headers=headers or {})

    def send(self, request: httpx.Request, *, follow_redirects: bool | None = None) -> _StatusResponse:
        # A8 F2: the layer always sends with ``follow_redirects=False`` and walks
        # the hops itself, so the stand-in has to accept that keyword.
        return self.get(str(request.url))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_flutter_status_reports_politeness_counters(monkeypatch):
    monkeypatch.setattr(
        search_mod, "load_index",
        lambda **kw: {"built_at": "2026-01-01T00:00:00+00:00", "entries": [{"name": "ListView"}]},
    )
    monkeypatch.setattr(server_mod.httpx, "Client", _StatusClient)

    out = flutter_status()

    # The public shape of the tool is unchanged…
    assert set(out["checks"]) == {"search_index", "cache", "api_flutter_dev", "pub_dev"}
    assert out["overall"] == "ok"
    # …and the counters ride along as an extra top-level block.
    pol = out["politeness"]
    assert pol["status"] == "ok"
    assert pol["disabled"] is False
    assert pol["requests"] >= 2          # the two probes went through the layer
    assert pol["robots_rows"] == 0       # fresh tmp robots DB
    for key in (
        "robots_fetches", "robots_cache_hits", "blocked_by_robots", "throttle_waits",
        "throttle_sleep_s", "host_delays", "budgets", "budget_denied",
        "revalidated_304", "retries_429", "retries_transport", "stalls", "errors",
    ):
        assert key in pol, key


def test_flutter_status_reports_a_disabled_layer(monkeypatch):
    monkeypatch.setenv("FLUTTER_DOCS_MCP_POLITENESS_DISABLED", "1")
    fetchers_mod.set_politeness(fast_layer())
    monkeypatch.setattr(
        search_mod, "load_index",
        lambda **kw: {"built_at": "x", "entries": [{"name": "ListView"}]},
    )
    monkeypatch.setattr(server_mod.httpx, "Client", _StatusClient)

    out = flutter_status()

    assert out["politeness"]["disabled"] is True
    assert out["overall"] == "ok"  # diagnostics never degrade the verdict


# ---------------------------------------------------------------------------
# A13 B3 — an unwritable cache directory must never take a tool down
# ---------------------------------------------------------------------------
def test_pub_package_and_status_survive_an_unwritable_cache_dir(monkeypatch, tmp_path):
    """A12 F-A12-2 regression: a read-only cache dir used to kill every tool.

    A12: with an unwritable cache directory the schema migration (which ran on
    *every* connection) raised ``OperationalError: attempt to write a readonly
    database`` straight out of ``_cache()`` — on HEAD the same tool worked.  The
    cache is optional now: the tool still answers and ``flutter_status`` says
    the cache is unusable instead of claiming "ok".
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where a directory should be")
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(blocker / "cache"))
    monkeypatch.setattr(server_mod, "_CACHE", None)
    monkeypatch.setattr(server_mod, "_CACHE_ERROR", None)

    assert server_mod._cache() is None          # fails soft, does not raise

    use_transport(monkeypatch, fetchers_mod, Recorder({
        "/robots.txt": WELCOME_ROBOTS,
        "/api/packages/dio": PUB_API_JSON,
        "/packages/dio": PUB_PAGE_HTML,
    }))

    out = server_mod.pub_package("dio")
    assert "error" not in out, out
    assert out["name"] == "dio"
    assert "HTTP networking package" in out["readme"]

    monkeypatch.setattr(
        search_mod, "load_index",
        lambda **kw: {"built_at": "x", "entries": [{"name": "ListView"}]},
    )
    monkeypatch.setattr(server_mod.httpx, "Client", _StatusClient)
    status = server_mod.flutter_status()
    assert status["checks"]["cache"]["status"] == "error", status["checks"]["cache"]
    assert "error" in status["checks"]["cache"]     # the reason is spelled out
    assert status["overall"] == "degraded"          # announced, not hidden
