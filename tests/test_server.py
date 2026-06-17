"""Tests for chrome_wrapper_plugin.server — wiring layer.

Mocks _get_engine (for tool-surface tests) and individual collaborators
(for _get_engine lifecycle tests) so no real Chrome is launched.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import chrome_wrapper_plugin.server as server_module
import chrome_wrapper_plugin.state as state_module
from chrome_wrapper_plugin.cdp import CDPSession
from chrome_wrapper_plugin.server import ChromeEngine, _get_engine, get_instance_info
from chrome_wrapper_plugin.state import SessionState


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_engine(**overrides) -> ChromeEngine:
    defaults = dict(
        proc=None,
        port=9222,
        user_data_dir=Path("/tmp/udd"),
        session_id="test-session",
    )
    defaults.update(overrides)
    return ChromeEngine(**defaults)


# ── get_instance_info ─────────────────────────────────────────────────────────

def test_get_instance_info_keys():
    """get_instance_info returns a dict with all expected keys."""
    engine = _fake_engine()

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert set(info.keys()) == {
        "session_id", "pid", "port", "user_data_dir", "profile", "hwnd", "window_title"
    }


def test_get_instance_info_values_no_proc():
    """When proc is None (reattach case), pid in result is None."""
    engine = _fake_engine(proc=None, port=9333, session_id="sess-42")

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["pid"] is None
    assert info["port"] == 9333
    assert info["session_id"] == "sess-42"
    assert info["hwnd"] is None
    assert info["window_title"] is None


def test_get_instance_info_values_with_proc():
    """When proc is present, pid is proc.pid."""
    proc = mock.MagicMock(spec=subprocess.Popen)
    proc.pid = 42000
    engine = _fake_engine(proc=proc, port=9444, session_id="sess-99")

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["pid"] == 42000


def test_get_instance_info_hwnd_resolved():
    """When proc.pid is available, find_chrome_hwnd is called with it and hwnd/title forwarded."""
    proc = mock.MagicMock(spec=subprocess.Popen)
    proc.pid = 42000
    engine = _fake_engine(proc=proc, port=9444, session_id="sess-99")

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(
            server_module, "find_chrome_hwnd", return_value=(0x001A0042, "Google Chrome")
        ) as mock_hwnd,
    ):
        info = get_instance_info()

    mock_hwnd.assert_called_once_with(42000)
    assert info["hwnd"] == 0x001A0042
    assert info["window_title"] == "Google Chrome"


def test_get_instance_info_reattach_uses_saved_pid():
    """On the reattach path (proc=None), find_chrome_hwnd is called with the pid from saved state."""
    engine = _fake_engine(proc=None, port=9333, session_id="sess-42")
    saved = _make_session_state(pid=55555, session_id="sess-42")

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=saved),
        mock.patch.object(
            server_module, "find_chrome_hwnd", return_value=(None, None)
        ) as mock_hwnd,
    ):
        get_instance_info()

    mock_hwnd.assert_called_once_with(55555)


def test_get_instance_info_profile_from_saved_state():
    """profile field in result comes from saved SessionState.profile."""
    engine = _fake_engine(proc=None, port=9333, session_id="sess-42")
    saved = _make_session_state(pid=55555, session_id="sess-42", profile="/tmp/master")

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=saved),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["profile"] == "/tmp/master"


def test_get_instance_info_no_saved_state_profile_is_none():
    """When load_state returns None, profile/hwnd/window_title are all None and
    find_chrome_hwnd is not called (no pid to look up)."""
    engine = _fake_engine(proc=None, port=9333, session_id="sess-42")

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(
            server_module, "find_chrome_hwnd", return_value=(None, None)
        ) as mock_hwnd,
    ):
        info = get_instance_info()

    mock_hwnd.assert_not_called()
    assert info["profile"] is None
    assert info["hwnd"] is None
    assert info["window_title"] is None


# ── _get_engine lifecycle ─────────────────────────────────────────────────────
#
# Each test resets server_module._engine to None in setup so we exercise the
# full lazy-init path without interference from other tests.

def _make_session_state(**overrides) -> SessionState:
    defaults = dict(
        session_id="eng-session",
        pid=55555,
        port=9300,
        user_data_dir="/tmp/eng_udd",
        profile="/tmp/master",
        created_at="2026-01-01T00:00:00",
    )
    defaults.update(overrides)
    return SessionState(**defaults)


class TestGetEngineReattach:
    """load_state returns a state with a live PID → reattach, no launch."""

    def setup_method(self):
        server_module._engine = None

    def test_reattach_returns_engine_with_no_proc(self, monkeypatch):
        live_state = _make_session_state(port=9300)

        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.load_state", return_value=live_state
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.is_process_alive", return_value=True
            ),
            mock.patch("chrome_wrapper_plugin.server.wait_for_cdp"),
            mock.patch(
                "chrome_wrapper_plugin.server.launch_chrome"
            ) as mock_launch,
            mock.patch.object(CDPSession, "__init__", return_value=None),
            mock.patch.object(CDPSession, "connect", return_value=None),
            mock.patch(
                "chrome_wrapper_plugin.server._attach_buffers"
            ) as mock_attach_buffers,
        ):
            engine = _get_engine()

        assert engine.proc is None
        assert engine.port == 9300
        assert engine.session_id == "eng-session"
        mock_launch.assert_not_called()
        mock_attach_buffers.assert_called_once_with(engine)

    def teardown_method(self):
        server_module._engine = None


class TestGetEngineFreshLaunch:
    """load_state returns state with dead PID → fresh launch path."""

    def setup_method(self):
        server_module._engine = None

    def test_fresh_launch_calls_launch_chrome_and_saves_state(
        self, tmp_path, monkeypatch
    ):
        dead_state = _make_session_state(pid=99999, port=9301)

        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.load_state", return_value=dead_state
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.is_process_alive", return_value=False
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.find_free_port", return_value=9400
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.seed_profile"
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.launch_chrome", return_value=fake_proc
            ) as mock_launch,
            mock.patch("chrome_wrapper_plugin.server.wait_for_cdp"),
            mock.patch(
                "chrome_wrapper_plugin.server.save_state"
            ) as mock_save,
            mock.patch(
                "tempfile.mkdtemp", return_value=str(tmp_path / "udd")
            ),
            mock.patch.object(CDPSession, "__init__", return_value=None),
            mock.patch.object(CDPSession, "connect", return_value=None),
            mock.patch(
                "chrome_wrapper_plugin.server._attach_buffers"
            ) as mock_attach_buffers,
        ):
            engine = _get_engine()

        mock_launch.assert_called_once()
        assert mock_save.call_count == 1
        saved: SessionState = mock_save.call_args[0][0]
        assert saved.pid == 12345
        assert saved.port == 9400
        assert engine.port == 9400
        assert engine.proc is fake_proc
        mock_attach_buffers.assert_called_once_with(engine)

    def teardown_method(self):
        server_module._engine = None


class TestGetEngineCache:
    """With _engine already set, _get_engine() returns the cached object."""

    def setup_method(self):
        server_module._engine = None

    def test_cache_hit_skips_load_state(self, monkeypatch):
        cached = _fake_engine(port=9500, session_id="cached-session")
        server_module._engine = cached

        with mock.patch(
            "chrome_wrapper_plugin.server.load_state"
        ) as mock_load:
            result = _get_engine()

        assert result is cached
        mock_load.assert_not_called()

    def teardown_method(self):
        server_module._engine = None


# ── TestGetEngineAttachesSession ──────────────────────────────────────────────
#
# Verifies that _get_engine() always attaches a connected CDPSession, on both
# the reattach path and the fresh-launch path.

class TestGetEngineAttachesSession:
    """_get_engine() attaches a connected CDPSession on both lifecycle paths."""

    def setup_method(self):
        server_module._engine = None

    def teardown_method(self):
        server_module._engine = None

    def test_reattach_path_attaches_session(self, monkeypatch):
        """Reattach path: engine.session is a CDPSession and connect() called once."""
        live_state = _make_session_state(port=9300)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.load_state", return_value=live_state
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.is_process_alive", return_value=True
            ),
            mock.patch("chrome_wrapper_plugin.server.wait_for_cdp"),
            mock.patch("chrome_wrapper_plugin.server.launch_chrome"),
            mock.patch.object(CDPSession, "__init__", return_value=None) as mock_init,
            mock.patch.object(CDPSession, "connect", return_value=None) as mock_connect,
            mock.patch(
                "chrome_wrapper_plugin.server._attach_buffers"
            ) as mock_attach_buffers,
        ):
            engine = _get_engine()

        assert isinstance(engine.session, CDPSession)
        mock_connect.assert_called_once()
        mock_attach_buffers.assert_called_once_with(engine)

    def test_fresh_launch_path_attaches_session(self, monkeypatch, tmp_path):
        """Fresh-launch path: engine.session is a CDPSession and connect() called once."""
        dead_state = _make_session_state(pid=99999, port=9301)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.load_state", return_value=dead_state
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.is_process_alive", return_value=False
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.find_free_port", return_value=9400
            ),
            mock.patch("chrome_wrapper_plugin.server.seed_profile"),
            mock.patch(
                "chrome_wrapper_plugin.server.launch_chrome", return_value=fake_proc
            ),
            mock.patch("chrome_wrapper_plugin.server.wait_for_cdp"),
            mock.patch("chrome_wrapper_plugin.server.save_state"),
            mock.patch("tempfile.mkdtemp", return_value=str(tmp_path / "udd")),
            mock.patch.object(CDPSession, "__init__", return_value=None) as mock_init,
            mock.patch.object(CDPSession, "connect", return_value=None) as mock_connect,
            mock.patch(
                "chrome_wrapper_plugin.server._attach_buffers"
            ) as mock_attach_buffers,
        ):
            engine = _get_engine()

        assert isinstance(engine.session, CDPSession)
        mock_connect.assert_called_once()
        mock_attach_buffers.assert_called_once_with(engine)


# ── TestGetEnginePoisonedCache ────────────────────────────────────────────────
#
# Guards blocking-1: if CDPSession.connect() raises, _engine must stay None so
# the next call to _get_engine() retries rather than returning a broken engine.

class TestGetEnginePoisonedCache:
    """connect() failure must NOT cache a broken engine."""

    def setup_method(self):
        server_module._engine = None

    def teardown_method(self):
        server_module._engine = None

    def test_reattach_connect_failure_leaves_engine_none(self, monkeypatch):
        """Reattach path: connect() raises → _engine stays None."""
        live_state = _make_session_state(port=9300)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.load_state", return_value=live_state
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.is_process_alive", return_value=True
            ),
            mock.patch("chrome_wrapper_plugin.server.wait_for_cdp"),
            mock.patch.object(CDPSession, "__init__", return_value=None),
            mock.patch.object(
                CDPSession, "connect", side_effect=RuntimeError("CDP handshake failed")
            ),
        ):
            with pytest.raises(RuntimeError, match="CDP handshake failed"):
                _get_engine()

        assert server_module._engine is None

    def test_fresh_launch_connect_failure_leaves_engine_none(
        self, monkeypatch, tmp_path
    ):
        """Fresh-launch path: connect() raises → _engine stays None."""
        dead_state = _make_session_state(pid=99999, port=9301)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.load_state", return_value=dead_state
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.is_process_alive", return_value=False
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.find_free_port", return_value=9400
            ),
            mock.patch("chrome_wrapper_plugin.server.seed_profile"),
            mock.patch(
                "chrome_wrapper_plugin.server.launch_chrome", return_value=fake_proc
            ),
            mock.patch("chrome_wrapper_plugin.server.wait_for_cdp"),
            mock.patch("chrome_wrapper_plugin.server.save_state"),
            mock.patch("tempfile.mkdtemp", return_value=str(tmp_path / "udd")),
            mock.patch.object(CDPSession, "__init__", return_value=None),
            mock.patch.object(
                CDPSession, "connect", side_effect=RuntimeError("CDP handshake failed")
            ),
        ):
            with pytest.raises(RuntimeError, match="CDP handshake failed"):
                _get_engine()

        assert server_module._engine is None
