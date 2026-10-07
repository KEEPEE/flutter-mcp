"""Shared offline fixtures for the whole test suite.

Two things must be true for every test in this repo, and both are easy to
forget, so they are autouse here rather than repeated per module:

1. **No real cache directory.** ``FLUTTER_DOCS_MCP_CACHE_DIR`` points at a
   per-test temp dir, so tests never read or write the developer's
   ``~/.cache/flutter-docs-mcp`` (a warm real cache makes tests order-dependent).
2. **A fresh politeness layer.** The process-wide singleton is replaced before
   each test with one backed by a temp robots DB, zero base delay and a seeded
   RNG — no real sleeping, no robots state leaking between tests — and reset
   afterwards so production code can never see a test instance.

Nothing in this file performs network I/O.
"""

from __future__ import annotations

import random

import pytest

from flutter_docs_mcp import fetchers as fetchers_mod
from flutter_docs_mcp.politeness import DEFAULT_DISABLE_ENV_VAR, Politeness


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    """Keep every test out of the real ``~/.cache/flutter-docs-mcp``."""
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    # An opt-out set in a developer's shell must not silently disable the
    # layer for the suite. Tests that want it off set it themselves.
    monkeypatch.delenv(DEFAULT_DISABLE_ENV_VAR, raising=False)
    yield


@pytest.fixture(autouse=True)
def politeness_layer():
    """Fresh, fast, deterministic politeness layer for the duration of a test.

    ``cache_path=":memory:"`` keeps the robots cache in-process: a SQLite file
    per test costs ~5 s across the suite and buys nothing here. File-backed
    persistence is covered explicitly in ``test_politeness_wiring.py``.
    """
    layer = Politeness(
        fetchers_mod.USER_AGENT,
        cache_path=":memory:",
        base_delay=(0.0, 0.0),  # no throttling sleeps in wiring tests
        rng=random.Random(1234),
    )
    fetchers_mod.set_politeness(layer)
    try:
        yield layer
    finally:
        fetchers_mod.set_politeness(None)
        layer.close()
