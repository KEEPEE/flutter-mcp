"""Offline tests for flutter_docs_mcp.cache.DocCache."""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from flutter_docs_mcp.cache import DocCache, default_db_path


def test_set_get_roundtrip(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    assert cache.get("missing") is None
    cache.set("k", "v1", ttl_seconds=60)
    assert cache.get("k") == "v1"


def test_ttl_expiry(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set("k", "v", ttl_seconds=1)
    assert cache.get("k") == "v"
    time.sleep(1.2)
    assert cache.get("k") is None


def test_stats_counts_without_deleting(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set("fresh1", "a", ttl_seconds=3600)
    cache.set("fresh2", "b", ttl_seconds=3600)
    cache.set("old", "c", ttl_seconds=-1)  # already in the past
    stats = cache.stats()
    assert stats == {"entries": 3, "expired": 1}
    # Expired rows are still physically present.
    assert cache.peek("old") == "c"


def test_overwrite_same_key(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set("k", "v1", ttl_seconds=3600)
    cache.set("k", "v2", ttl_seconds=7200)
    assert cache.get("k") == "v2"
    assert cache.stats()["entries"] == 1


def test_default_db_path_honors_env(monkeypatch, tmp_path):
    cache_dir = tmp_path / "custom-cache"
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(cache_dir))
    assert default_db_path() == os.path.join(str(cache_dir), "cache.db")

    cache = DocCache()  # no explicit path → env var
    assert cache.db_path == os.path.join(str(cache_dir), "cache.db")
    cache.set("k", "v", ttl_seconds=60)
    assert (cache_dir / "cache.db").exists()


def test_peek_ignores_expiry(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set("k", "v", ttl_seconds=-1)
    assert cache.get("k") is None
    assert cache.peek("k") == "v"


# ---------------------------------------------------------------------------
# Schema migration: a database written before the politeness layer landed
# ---------------------------------------------------------------------------

OLD_SCHEMA_SQL = "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, expires_at REAL)"


def _write_old_schema(db_path: str) -> None:
    """Create exactly the pre-politeness table and one warm row."""
    conn = sqlite3.connect(db_path)
    conn.execute(OLD_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?)",
        ("https://api.flutter.dev/flutter/widgets/ListView-class.html",
         '{"ok": true, "markdown": "# ListView"}',
         time.time() + 3600),
    )
    conn.commit()
    conn.close()


def _columns(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(kv)")}
    finally:
        conn.close()


def test_opening_an_old_database_migrates_it_in_place(tmp_path):
    db = str(tmp_path / "cache.db")
    _write_old_schema(db)
    assert _columns(db) == {"key", "value", "expires_at"}

    cache = DocCache(db_path=db)  # opening must migrate, not recreate

    assert {"etag", "last_modified", "body"} <= _columns(db)
    # The warm row survived the upgrade untouched — no data loss.
    assert cache.get(
        "https://api.flutter.dev/flutter/widgets/ListView-class.html"
    ) == '{"ok": true, "markdown": "# ListView"}'
    # Old rows simply have no validators yet.
    entry = cache.get_entry(
        "https://api.flutter.dev/flutter/widgets/ListView-class.html", include_expired=True
    )
    assert entry["etag"] is None and entry["last_modified"] is None and entry["body"] is None


def test_migration_is_idempotent_and_never_rewrites_rows(tmp_path):
    db = str(tmp_path / "cache.db")
    _write_old_schema(db)
    before = sqlite3.connect(db).execute("SELECT rowid, value FROM kv").fetchall()

    for _ in range(3):
        DocCache(db_path=db)  # reopening must not error or duplicate columns

    assert _columns(db) == {"key", "value", "expires_at", "etag", "last_modified", "body"}
    after = sqlite3.connect(db).execute("SELECT rowid, value FROM kv").fetchall()
    assert after == before  # same rowids, same values
    assert DocCache(db_path=db).stats() == {"entries": 1, "expired": 0}


def test_migrated_database_still_works_for_plain_reads_and_writes(tmp_path):
    db = str(tmp_path / "cache.db")
    _write_old_schema(db)
    cache = DocCache(db_path=db)
    cache.set("fresh", "v", ttl_seconds=60)
    assert cache.get("fresh") == "v"
    assert cache.stats()["entries"] == 2


# ---------------------------------------------------------------------------
# Validator / raw-body columns (conditional GET support)
# ---------------------------------------------------------------------------

def test_set_stores_and_clears_validators(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set("u", "parsed", ttl_seconds=60, etag='"e1"', last_modified="Tue, 06 Oct 2026", body="<html>")
    entry = cache.get_entry("u")
    assert entry["etag"] == '"e1"'
    assert entry["last_modified"] == "Tue, 06 Oct 2026"
    assert entry["body"] == "<html>"

    # A plain set() overwrites the whole row, validators included: a stale
    # validator must never outlive the response it came from.
    cache.set("u", "parsed2", ttl_seconds=60)
    assert cache.get_entry("u")["etag"] is None


def test_set_value_preserves_validators_written_by_the_fetcher(tmp_path):
    """The server caches the parsed result; the fetcher's row data survives."""
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set_validators("u", etag='"e1"', last_modified="Tue, 06 Oct 2026", body="<html>body</html>")
    cache.set_value("u", '{"ok": true}', ttl_seconds=60)

    entry = cache.get_entry("u")
    assert entry["value"] == '{"ok": true}'
    assert entry["etag"] == '"e1"' and entry["body"] == "<html>body</html>"
    assert cache.get("u") == '{"ok": true}'


def test_validators_only_row_never_answers_get(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set_validators("u", etag='"e1"', body="raw")
    assert cache.get("u") is None                       # no parsed value yet
    assert cache.get_entry("u")["body"] == "raw"        # but revalidation works


def test_get_entry_honours_expiry_unless_asked_otherwise(tmp_path):
    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    cache.set("gone", "v", ttl_seconds=-1, etag='"e1"', body="raw")
    assert cache.get_entry("gone") is None
    stale = cache.get_entry("gone", include_expired=True)
    assert stale["expired"] is True and stale["etag"] == '"e1"'


# ---------------------------------------------------------------------------
# A13 B3 — the migration runs once; an unusable cache address fails upstream
# ---------------------------------------------------------------------------

def test_schema_migration_runs_once_per_cache(tmp_path, monkeypatch):
    """A12 F-A12-3: ``_ensure_schema`` used to run on *every* connection.

    Functionally fine, but it put a ``PRAGMA table_info`` and a possible
    ``ALTER TABLE`` in front of every single read — and that was exactly the
    line that blew up (``attempt to write a readonly database``) for a cache
    directory the user cannot write.  It now runs once, at construction.
    """
    calls: list[str] = []
    original = DocCache._ensure_schema.__func__

    def counting(cls, conn):
        calls.append("run")
        return original(cls, conn)

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(counting))

    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    for i in range(10):
        cache.set(f"k{i}", "v", ttl_seconds=60)
        cache.get(f"k{i}")
        cache.get_entry(f"k{i}", include_expired=True)
        cache.set_value(f"k{i}", "w", ttl_seconds=60)
        cache.set_validators(f"k{i}", etag='"x"', last_modified=None, body=None, ttl_seconds=60)
        cache.peek(f"k{i}")
        cache.stats()

    assert calls == ["run"], f"_ensure_schema ran {len(calls)} times, expected exactly once"


def test_one_shot_migration_is_still_idempotent(tmp_path):
    """Idempotence is kept on purpose: a re-run must be a harmless no-op."""
    db = str(tmp_path / "cache.db")
    cache = DocCache(db_path=db)
    for _ in range(2):
        conn = sqlite3.connect(db)
        try:
            DocCache._ensure_schema(conn)
        finally:
            conn.close()
    cache.set("k", "v", ttl_seconds=60)
    assert cache.get("k") == "v"


def test_unusable_cache_address_raises_at_construction(tmp_path):
    """The contract all four repos rely on: fail **fast**, in ``__init__``.

    Every repo wraps ``DocCache()`` in try/except and degrades to no cache, so
    the failure has to surface here — not later, inside a tool call, on the
    first read that happened to need a migration (A12 F-A12-2).
    """
    with pytest.raises(sqlite3.OperationalError):
        DocCache(db_path=str(tmp_path))  # a directory is not a writable database
