"""Tests for chrome_wrapper_plugin.state.

All tests use monkeypatching and tmp_path — no real Chrome, no real registry.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import chrome_wrapper_plugin.state as state_module
from chrome_wrapper_plugin.state import (
    SessionState,
    delete_state,
    is_process_alive,
    load_state,
    reap_orphans,
    resolve_session_id,
    save_state,
)


# ── resolve_session_id ────────────────────────────────────────────────────────

def test_session_id_env_var_priority(monkeypatch):
    """CLAUDE_SESSION_ID wins over all other env-vars."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-abc")
    monkeypatch.setenv("ANTHROPIC_SESSION_ID", "anthropic-abc")
    monkeypatch.setenv("MCP_SESSION_ID", "mcp-abc")
    assert resolve_session_id() == "claude-abc"


def test_session_id_fallback_chain(monkeypatch):
    """Second env-var is used when CLAUDE_SESSION_ID is absent."""
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_SESSION_ID", "anthropic-xyz")
    monkeypatch.setenv("MCP_SESSION_ID", "mcp-xyz")
    assert resolve_session_id() == "anthropic-xyz"


def test_session_id_deterministic_fallback(monkeypatch):
    """Fallback ID is sha256(host:user)[:16] and stable across calls."""
    for var in ("CLAUDE_SESSION_ID", "ANTHROPIC_SESSION_ID", "MCP_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)

    key = socket.gethostname() + ":" + getpass.getuser()
    expected = hashlib.sha256(key.encode()).hexdigest()[:16]

    assert resolve_session_id() == expected
    # Stable across calls
    assert resolve_session_id() == expected


# ── _state_dir ────────────────────────────────────────────────────────────────

def test_state_dir_uses_claude_plugin_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    # Access via the module-level function (must reset cached state dir)
    result = state_module._state_dir()
    assert result == tmp_path / "instances"
    assert result.is_dir()


def test_state_dir_falls_back_to_tmpdir(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    result = state_module._state_dir()
    # Should be inside the system temp directory
    assert str(result).startswith(tempfile.gettempdir())
    assert result.name == "instances"


def test_state_dir_not_under_cwd(monkeypatch):
    """State directory must never be inside the current working directory."""
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    result = state_module._state_dir()
    cwd = Path.cwd()
    assert not str(result).startswith(str(cwd)), (
        f"State dir {result!r} must not be under cwd {cwd!r}"
    )


# ── SessionState serialization ────────────────────────────────────────────────

def _make_state(**overrides) -> SessionState:
    defaults = dict(
        session_id="test-session",
        pid=12345,
        port=9222,
        user_data_dir="/tmp/udd",
        profile="/tmp/master",
        created_at="2026-01-01T00:00:00",
    )
    defaults.update(overrides)
    return SessionState(**defaults)


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    st = _make_state()
    save_state(st)
    loaded = load_state("test-session")

    assert loaded is not None
    assert loaded.session_id == st.session_id
    assert loaded.pid == st.pid
    assert loaded.port == st.port
    assert loaded.user_data_dir == st.user_data_dir
    assert loaded.profile == st.profile
    assert loaded.created_at == st.created_at


def test_load_returns_none_for_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    assert load_state("does-not-exist") is None


def test_load_handles_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    # Write invalid JSON directly
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    (instances_dir / "bad-session.json").write_text("not json!!!")
    result = load_state("bad-session")
    assert result is None


# ── is_process_alive ──────────────────────────────────────────────────────────

def test_is_process_alive_current_process():
    """The current process is alive."""
    assert is_process_alive(os.getpid()) is True


def test_is_process_alive_dead_pid():
    """A dead PID (no such process) returns False.

    On Windows, OpenProcess returns a null handle for a non-existent PID with
    GetLastError() == ERROR_INVALID_PARAMETER (87).  We simulate this via
    ctypes mocking so the test is deterministic and not subject to the Windows
    behaviour where a PID remains queryable until all OS handles to it are closed
    (including the one Popen holds internally while the Popen object is alive).
    """
    import ctypes

    # Patch windll.kernel32 only when it exists (i.e. on Windows).
    if not hasattr(ctypes, "windll"):
        pytest.skip("Windows-only test")

    ERROR_INVALID_PARAMETER = 87  # GetLastError value for non-existent PID

    with (
        mock.patch.object(
            ctypes.windll.kernel32, "OpenProcess", return_value=None
        ),
        mock.patch.object(
            ctypes.windll.kernel32,
            "GetLastError",
            return_value=ERROR_INVALID_PARAMETER,
        ),
    ):
        assert is_process_alive(999999999) is False


# ── reap_orphans ──────────────────────────────────────────────────────────────

def test_reap_orphans_removes_dead_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    dead_udd = tmp_path / "dead_udd"
    dead_udd.mkdir()
    dead_state = _make_state(session_id="dead-session", pid=999999999, user_data_dir=str(dead_udd))
    save_state(dead_state)

    with mock.patch.object(state_module, "is_process_alive", return_value=False):
        reaped = reap_orphans()

    assert "dead-session" in reaped
    assert not (tmp_path / "instances" / "dead-session.json").exists()
    assert not dead_udd.exists()


def test_reap_orphans_keeps_live_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    live_udd = tmp_path / "live_udd"
    live_udd.mkdir()
    live_state = _make_state(session_id="live-session", pid=os.getpid(), user_data_dir=str(live_udd))
    save_state(live_state)

    with mock.patch.object(state_module, "is_process_alive", return_value=True):
        reaped = reap_orphans()

    assert "live-session" not in reaped
    assert (tmp_path / "instances" / "live-session.json").exists()
    assert live_udd.exists()


def test_reap_orphans_skips_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    (instances_dir / "corrupt.json").write_text("{{broken")

    # Must not raise — corrupt files are skipped
    reaped = reap_orphans()
    assert "corrupt" not in reaped
