"""Offline tests for flutter_docs_mcp.cache.DocCache."""

from __future__ import annotations

import os
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
