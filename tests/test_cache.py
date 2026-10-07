"""Offline tests for flutter_docs_mcp.cache.DocCache."""

from __future__ import annotations

import os
import sqlite3
import time
import urllib.request

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


# ---------------------------------------------------------------------------
# P5 — a read-only database degrades the cache, it must never break a tool
# ---------------------------------------------------------------------------
# Why the two ``chmod`` tests below are skipped for root: root bypasses the DAC
# permission bits, so ``chmod 0444`` does not produce a read-only database for
# it and the test would go green without ever entering the read-only code path.
# That masking is exactly what hid this bug — the suite was green locally (DSH
# runs as root) while GitHub's non-root runner failed on the schema migration
# with ``sqlite3.OperationalError: attempt to write a readonly database``.
# ``test_read_only_mode_is_forced_even_for_root`` forces the same path with the
# uid taken out of the equation, so both kinds of runner are covered.


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_read_only_database_is_still_readable(tmp_path):
    """A root-owned or 0444 ``cache.db`` stays readable; writes become no-ops."""
    db = str(tmp_path / "cache.db")
    _write_old_schema(db)
    os.chmod(db, 0o444)
    try:
        cache = DocCache(db_path=db)
        assert cache.read_only is True
        assert cache.read_only_reason  # spelled out for ``*_status()``
        assert cache.get("https://api.flutter.dev/flutter/widgets/ListView-class.html") == '{"ok": true, "markdown": "# ListView"}'
        # The additive migration was skipped because nothing can be written, so
        # the columns it would have added read as NULL instead of raising
        # ``no such column: etag``.
        entry = cache.get_entry("https://api.flutter.dev/flutter/widgets/ListView-class.html", include_expired=True)
        assert entry is not None and entry["etag"] is None and entry["body"] is None
        assert _columns(db) == {"key", "value", "expires_at"}  # the file is untouched
        # Writes are a silent no-op returning False — the cache is an
        # optimisation, never a required dependency.
        assert cache.set("k", "v", ttl_seconds=60) is False
        assert cache.set_value("k", "v", ttl_seconds=60) is False
        assert cache.set_validators("k", etag='"e1"') is False
        assert cache.get("k") is None and cache.peek("k") is None
        assert cache.stats() == {"entries": 1, "expired": 0}
    finally:
        os.chmod(db, 0o644)  # let tmp_path cleanup work


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_read_only_directory_is_still_readable(tmp_path):
    """The journal lives next to the DB, so an unwritable dir blocks writes too."""
    cache_dir = tmp_path / "cache-dir"
    cache_dir.mkdir()
    db = str(cache_dir / "cache.db")
    warm = DocCache(db_path=db)
    assert warm.set("k", "v", ttl_seconds=3600) is True

    os.chmod(cache_dir, 0o555)
    try:
        cache = DocCache(db_path=db)
        assert cache.read_only is True
        assert cache.get("k") == "v"
        assert cache.set("k2", "v2", ttl_seconds=60) is False
        assert cache.get("k2") is None
        assert cache.stats() == {"entries": 1, "expired": 0}
    finally:
        os.chmod(cache_dir, 0o755)


def test_read_only_mode_is_forced_even_for_root(tmp_path, monkeypatch):
    """Deterministic version of the chmod test — it must pass whatever the uid.

    ``chmod`` is meaningless for root, so the migration itself is made to raise
    the exact error SQLite raises for a database the process cannot write.
    """
    db = str(tmp_path / "cache.db")
    _write_old_schema(db)

    def refusing_migration(cls, conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(refusing_migration))

    cache = DocCache(db_path=db)
    assert cache.read_only is True
    assert "readonly" in (cache.read_only_reason or "")
    # reads still work …
    assert cache.get("https://api.flutter.dev/flutter/widgets/ListView-class.html") == '{"ok": true, "markdown": "# ListView"}'
    # … the columns the skipped migration would have added read as NULL …
    entry = cache.get_entry("https://api.flutter.dev/flutter/widgets/ListView-class.html", include_expired=True)
    assert entry is not None and entry["etag"] is None and entry["body"] is None
    # … and every write is a no-op, never an exception.
    assert cache.set("k", "v", ttl_seconds=60) is False
    assert cache.set_value("k", "v", ttl_seconds=60) is False
    assert cache.set_validators("k", etag='"e1"') is False
    assert cache.get("k") is None and cache.peek("k") is None
    assert cache.stats() == {"entries": 1, "expired": 0}
    # every later connection really is opened through the ``mode=ro`` URI:
    # SQLite itself refuses the write, whatever the uid of the process.
    conn = cache._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv (key, value) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_unwritable_directory_is_detected_without_chmod(tmp_path, monkeypatch):
    """Same as the read-only-directory test, forced so that root covers it.

    The up-front ``os.access`` hint is stubbed to report what it reports on a
    non-root runner: the directory cannot hold SQLite's journal.
    """
    db = str(tmp_path / "cache.db")
    warm = DocCache(db_path=db)
    assert warm.read_only is False
    assert warm.set("k", "v", ttl_seconds=3600) is True

    monkeypatch.setattr(DocCache, "_write_possible", lambda self: False)
    cache = DocCache(db_path=db)
    assert cache.read_only is True
    assert cache.get("k") == "v"
    assert cache.set("k2", "v2", ttl_seconds=60) is False
    assert cache.get("k2") is None


def test_refused_write_downgrades_the_cache_to_read_only(tmp_path, monkeypatch):
    """Safety net for what the up-front check cannot see.

    Permissions can change after construction, and ``os.access`` is a lie for
    root.  The first write SQLite refuses flips the cache to read-only instead
    of raising out of the tool that merely wanted to cache something.
    """
    db = str(tmp_path / "cache.db")
    cache = DocCache(db_path=db)
    assert cache.set("k", "v", ttl_seconds=3600) is True

    # A ``mode=ro`` connection refuses writes for every uid, exactly like a
    # database the process has no write permission for.
    monkeypatch.setattr(
        DocCache,
        "_connect",
        lambda self: sqlite3.connect(
            f"file:{urllib.request.pathname2url(self.db_path)}?mode=ro", uri=True
        ),
    )
    assert cache.set("k2", "v2", ttl_seconds=60) is False
    assert cache.read_only is True
    assert "readonly" in (cache.read_only_reason or "")
    assert cache.get("k") == "v"  # reads are unaffected
    # later writes are skipped up front, without touching SQLite again
    assert cache.set("k3", "v3", ttl_seconds=60) is False
    assert cache.get("k3") is None


def test_read_only_database_without_a_table_reads_as_empty(tmp_path, monkeypatch):
    """A 0-byte ``cache.db`` that cannot be migrated has no ``kv`` table at all.

    That is still not an error: every read is a miss and every write a no-op.
    """
    db = tmp_path / "cache.db"
    db.write_bytes(b"")
    monkeypatch.setattr(DocCache, "_write_possible", lambda self: False)

    cache = DocCache(db_path=str(db))
    assert cache.read_only is True
    assert cache.get("k") is None
    assert cache.get_entry("k", include_expired=True) is None
    assert cache.peek("k") is None
    assert cache.stats() == {"entries": 0, "expired": 0}
    assert cache.set("k", "v", ttl_seconds=60) is False


def test_forced_migration_failure_still_raises_when_nothing_is_readable(tmp_path, monkeypatch):
    """Read-only mode needs a database that *can* be read.

    A path that is not a database at all has nothing to fall back to, so the
    error still propagates from ``__init__`` and the caller drops the cache —
    the A13 B3 contract every repo's ``try/except DocCache()`` relies on.
    """
    def refusing_migration(cls, conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(refusing_migration))
    # A directory is not a database: nothing can be opened, read or written.
    with pytest.raises(sqlite3.OperationalError):
        DocCache(db_path=str(tmp_path))
