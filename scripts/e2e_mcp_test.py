#!/usr/bin/env python3
"""End-to-end test for the flutter-docs MCP server over stdio JSON-RPC.

Spawns the installed console script (``flutter-docs``) as a subprocess and
speaks newline-delimited JSON-RPC to it: initialize → notifications/initialized
→ tools/list → tools/call for every tool (including one negative case) →
health_check at the very end to prove the server is still alive after an error
response.

Stdlib only — no project imports. Exit code 0 only if every check passes.
Per-call timeout is generous (120s): the first flutter_docs call may trigger a
search-index build plus live fetches.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

#: Which server process to spawn.  Override with ``FLUTTER_DOCS_MCP_E2E_CMD``
#: (a shell-split command); otherwise use this checkout's ``.venv`` if it has
#: the console script, else run the module with the interpreter that started
#: this script (works with any install, editable or not).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_BIN = os.path.join(_REPO_ROOT, ".venv", "bin", "flutter-docs")
if os.environ.get("FLUTTER_DOCS_MCP_E2E_CMD"):
    SERVER_CMD = os.environ["FLUTTER_DOCS_MCP_E2E_CMD"].split()
elif os.path.exists(_LOCAL_BIN):
    SERVER_CMD = [_LOCAL_BIN]
else:
    SERVER_CMD = [sys.executable, "-m", "flutter_docs_mcp.server"]
PROTOCOL_VERSION = "2025-03-26"
PER_CALL_TIMEOUT = 120.0

EXPECTED_TOOLS = {
    "health_check",
    "flutter_docs",
    "flutter_search",
    "flutter_mentions",
    "pub_package",
    "flutter_status",
}


class McpClient:
    """Minimal newline-delimited JSON-RPC client over a subprocess stdio."""

    def __init__(self, cmd):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 0
        self._lines: "queue.Queue[str]" = queue.Queue()
        self.stderr_chunks: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_chunks.append(line)

    def send_notification(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params: dict | None = None,
                timeout: float = PER_CALL_TIMEOUT):
        self._next_id += 1
        mid = self._next_id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {timeout}s waiting for '{method}'")
            line = self._lines.get(timeout=remaining)
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue  # ignore non-JSON noise on stdout
            if data.get("id") != mid:
                continue  # not our response (sequential calls → none expected)
            if "error" in data:
                raise RuntimeError(f"JSON-RPC error for {method}: {data['error']}")
            return data["result"]

    def _write(self, obj) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass


def call_tool(client: McpClient, name: str, arguments: dict) -> dict:
    """tools/call → the parsed dict the tool returned.

    Raises on protocol errors or when the tool reports isError; a tool that
    returns its own {"error": ...} dict is NOT an error here (isError stays
    false) and is returned as-is.
    """
    result = client.request("tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        raise RuntimeError(f"tool '{name}' reported isError: {_first_text(result)[:300]}")
    return json.loads(_first_text(result))


def _first_text(result) -> str:
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            return item.get("text", "")
    raise RuntimeError(f"no text content in tools/call result: {json.dumps(result)[:300]}")


def main() -> int:
    client = McpClient(SERVER_CMD)
    results: list[tuple[str, bool, str]] = []

    def record(label: str, ok: bool, detail: str) -> None:
        results.append((label, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {detail}")

    try:
        # 1. initialize --------------------------------------------------------
        init = client.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "flutter-docs-e2e", "version": "0.1"},
        })
        record(
            "initialize",
            bool(init.get("serverInfo")),
            f"server={init.get('serverInfo', {}).get('name')} protocol={init.get('protocolVersion')}",
        )
        client.send_notification("notifications/initialized")

        # 2. tools/list ----------------------------------------------------------
        listed = client.request("tools/list", {})
        names = {t.get("name") for t in listed.get("tools", [])}
        missing = EXPECTED_TOOLS - names
        record(
            "tools/list",
            not missing,
            f"tools={sorted(names)}" + (f" missing={sorted(missing)}" if missing else ""),
        )

        # 3. flutter_status --------------------------------------------------------
        data = call_tool(client, "flutter_status", {})
        checks = data.get("checks", {})
        ok = (
            isinstance(checks, dict)
            and {"search_index", "cache", "api_flutter_dev", "pub_dev"} <= set(checks)
            and data.get("overall") in ("ok", "degraded")
        )
        si = checks.get("search_index", {}) if isinstance(checks, dict) else {}
        record(
            'flutter_status {}',
            ok,
            f"overall={data.get('overall')} index_entries={si.get('entries')} "
            f"stale={si.get('stale')} cache_entries={checks.get('cache', {}).get('entries')}",
        )

        # 4. flutter_search -----------------------------------------------------------
        data = call_tool(client, "flutter_search", {"query": "text field", "limit": 3})
        results_list = data.get("results")
        ok = (
            data.get("query") == "text field"
            and isinstance(results_list, list)
            and data.get("count") == len(results_list or [])
            and data["count"] >= 1
            and "index_stale" in data
        )
        top = results_list[0] if results_list else {}
        record(
            'flutter_search {"query": "text field", "limit": 3}',
            ok,
            f"count={data.get('count')} top={top.get('name')} score={top.get('score')} "
            f"stale={data.get('index_stale')}",
        )

        # 5. flutter_docs ListView -------------------------------------------------------
        data = call_tool(client, "flutter_docs", {"identifier": "ListView"})
        ok = (
            data.get("type") == "flutter_class"
            and isinstance(data.get("content"), str)
            and len(data["content"]) > 100
            and "api.flutter.dev" in (data.get("url") or "")
        )
        record(
            'flutter_docs {"identifier": "ListView"}',
            ok,
            f"title={data.get('title')!r} content_len={len(data.get('content', ''))} "
            f"cached={data.get('cached')} truncated={data.get('truncated')}",
        )

        # 6. flutter_docs material.AppBar ----------------------------------------------------
        data = call_tool(client, "flutter_docs", {"identifier": "material.AppBar"})
        ok = (
            data.get("type") == "flutter_class"
            and str(data.get("title") or "").startswith("AppBar")
            and len(data.get("content", "")) > 100
        )
        record(
            'flutter_docs {"identifier": "material.AppBar"}',
            ok,
            f"title={data.get('title')!r} content_len={len(data.get('content', ''))} "
            f"cached={data.get('cached')}",
        )

        # 7. flutter_docs dart:async.Future -----------------------------------------------------
        data = call_tool(client, "flutter_docs", {"identifier": "dart:async.Future"})
        ok = (
            data.get("type") == "dart_class"
            and str(data.get("title") or "").startswith("Future")
            and len(data.get("content", "")) > 100
        )
        record(
            'flutter_docs {"identifier": "dart:async.Future"}',
            ok,
            f"title={data.get('title')!r} content_len={len(data.get('content', ''))} "
            f"cached={data.get('cached')}",
        )

        # 8. flutter_docs pub:dio ------------------------------------------------------------------
        data = call_tool(client, "flutter_docs", {"identifier": "pub:dio"})
        ok = (
            data.get("type") == "pub_package"
            and len(data.get("content", "")) > 50
            and "pub.dev" in (data.get("url") or "")
        )
        record(
            'flutter_docs {"identifier": "pub:dio"}',
            ok,
            f"title={data.get('title')!r} content_len={len(data.get('content', ''))} "
            f"cached={data.get('cached')}",
        )

        # 9. pub_package provider --------------------------------------------------------------------
        data = call_tool(client, "pub_package", {"package_name": "provider"})
        ok = (
            data.get("name") == "provider"
            and bool(data.get("version"))
            and isinstance(data.get("readme"), str)
            and len(data["readme"]) > 50
        )
        record(
            'pub_package {"package_name": "provider"}',
            ok,
            f"version={data.get('version')} likes={data.get('likes')} "
            f"pub_points={data.get('pub_points')} readme_len={len(data.get('readme', ''))} "
            f"cached={data.get('cached')}",
        )

        # 10. flutter_mentions — one entry per mention, constraint reported ----------
        mention_text = (
            "Stack: @flutter_mcp provider:^6.0.0 for state, "
            "@flutter_mcp dio:>=5.0.0 <6.0.0 for HTTP, @flutter_mcp material.AppBar."
        )
        data = call_tool(client, "flutter_mentions", {"text": mention_text, "max_tokens": 300})
        entries = data.get("results") or []
        ok = (
            data.get("mentions") == 3
            and len(entries) == 3
            and [e.get("mention") for e in entries] == [
                "@flutter_mcp provider:^6.0.0",
                "@flutter_mcp dio:>=5.0.0 <6.0.0",
                "@flutter_mcp material.AppBar",
            ]
            and all(e.get("content") for e in entries)
            and entries[0].get("requested_constraint") == "^6.0.0"
            and bool(entries[0].get("version"))
            and entries[2].get("type") == "flutter_class"
        )
        record(
            'flutter_mentions {3 mentions incl. two version constraints}',
            ok,
            f"mentions={data.get('mentions')} entries={len(entries)} "
            + " ".join(f"{e.get('type')}@{e.get('version')}" for e in entries),
        )

        # 11. flutter_mentions — a missing version is never substituted --------------
        data = call_tool(client, "flutter_mentions", {"text": "@flutter_mcp provider:6.9.9", "max_tokens": 200})
        entry = (data.get("results") or [{}])[0]
        ok = (
            data.get("mentions") == 1
            and len(data.get("results") or []) == 1
            and entry.get("type") == "not_found"
            and entry.get("version") is None
            and "not a published release" in str(entry.get("error"))
            and bool(entry.get("nearest_versions"))
        )
        record(
            'flutter_mentions {"text": "@flutter_mcp provider:6.9.9"}',
            ok,
            f"type={entry.get('type')} nearest={entry.get('nearest_versions')}",
        )

        # 12. negative case -----------------------------------------------------------------------------
        data = call_tool(client, "flutter_docs", {"identifier": "DefinitelyNotARealClassXYZ123"})
        ok = isinstance(data, dict) and "error" in data and "suggestion" in data
        record(
            'flutter_docs {"identifier": "DefinitelyNotARealClassXYZ123"}',
            ok,
            f"error={str(data.get('error'))[:140]!r}",
        )

        # 11. liveness after the error ---------------------------------------------------------------------
        data = call_tool(client, "health_check", {})
        ok = data.get("status") == "ok"
        record("health_check (liveness)", ok, f"version={data.get('version')}")
    except Exception as exc:
        record(f"exception during run ({exc.__class__.__name__})", False, str(exc)[:300])
    finally:
        client.close()

    failed = [r for r in results if not r[1]]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        stderr_tail = "".join(client.stderr_chunks[-20:])
        if stderr_tail.strip():
            print("--- server stderr (tail) ---")
            print(stderr_tail, end="")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
