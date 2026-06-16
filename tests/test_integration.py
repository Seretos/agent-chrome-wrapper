"""Integration test: launch the frozen binary, navigate to a URL, capture screenshot.

Windows-only. Skipped when:
  - not running on Windows (sys.platform != 'win32')
  - bin/chrome-wrapper.exe does not exist (binary not yet built)
  - Chrome is absent from the host (CHROME_WRAPPER_CHROME_PATH or default paths)

Run manually after `pwsh scripts/build.ps1`:
    pytest tests/test_integration.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXE = REPO_ROOT / "bin" / "chrome-wrapper.exe"


def _chrome_present() -> bool:
    """Return True when Chrome can be found via env-var or default install paths."""
    env_path = os.environ.get("CHROME_WRAPPER_CHROME_PATH", "").strip()
    if env_path and Path(env_path).is_file():
        return True
    rel = Path("Google") / "Chrome" / "Application" / "chrome.exe"
    for base_var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(base_var, "")
        if base and (Path(base) / rel).is_file():
            return True
    return False


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not EXE.exists() or not _chrome_present(),
    reason="integration test requires Windows, bin/chrome-wrapper.exe, and Chrome",
)


def _send_recv(proc: subprocess.Popen, msg: dict) -> dict:
    """Write one JSON-RPC line to *proc* stdin and block until a matching reply arrives."""
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        raw = proc.stdout.readline()
        if not raw:
            raise RuntimeError("binary closed stdout unexpectedly")
        try:
            resp = json.loads(raw.decode())
        except json.JSONDecodeError:
            continue
        if resp.get("id") == msg.get("id"):
            return resp
    raise TimeoutError(f"No response within 30 s for id={msg.get('id')!r}")


def test_navigate_and_screenshot():
    """Frozen binary: initialize -> navigate to example.com -> screenshot returns PNG image."""
    proc = subprocess.Popen(
        [str(EXE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        init = _send_recv(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "integration-test", "version": "1"},
            },
        })
        assert "result" in init, f"initialize failed: {init}"
        assert "protocolVersion" in init["result"]

        # MCP requires an 'initialized' notification before tool calls.
        proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n").encode()
        )
        proc.stdin.flush()

        nav = _send_recv(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "navigate", "arguments": {"url": "https://example.com"}},
        })
        assert "result" in nav, f"navigate failed: {nav}"

        ss = _send_recv(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "screenshot", "arguments": {}},
        })
        assert "result" in ss, f"screenshot failed: {ss}"
        content = ss["result"].get("content", [])
        assert any(c.get("type") == "image" for c in content), (
            f"Expected image content item, got: {content!r}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
