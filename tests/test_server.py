"""Tests for chrome_wrapper_plugin.server — wiring layer.

Mocks _get_engine (for tool-surface tests) and individual collaborators
(for _get_engine lifecycle tests) so no real Chrome is launched.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import chrome_wrapper_plugin.server as server_module
import chrome_wrapper_plugin.state as state_module
from chrome_wrapper_plugin.cdp import CDPError, CDPSession
from chrome_wrapper_plugin.server import ChromeEngine, _get_engine, get_instance_info
from chrome_wrapper_plugin.state import SessionState, load_state, save_state


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
        "session_id", "pid", "port", "user_data_dir", "profile", "hwnd", "window_title",
        "cdp_alive",
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


# ── get_instance_info: cdp_alive (ticket #29) ─────────────────────────────────

def test_get_instance_info_cdp_alive_true_when_probe_succeeds():
    """A reachable Chrome (Target.getTargetInfo round-trip succeeds) reports
    cdp_alive: True, and the probe uses the module's short probe timeout."""
    mock_session = mock.MagicMock(spec=CDPSession)
    mock_session.send.return_value = {"targetInfo": {"url": "about:blank"}}
    engine = _fake_engine(session=mock_session)

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["cdp_alive"] is True
    # Pin the literal expected value rather than referencing
    # server_module._CDP_PROBE_TIMEOUT — this would fail if the module
    # constant were ever changed to something other than 2.0.
    mock_session.send.assert_called_once_with(
        "Target.getTargetInfo", {}, timeout=2.0
    )


def test_get_instance_info_cdp_alive_false_when_probe_raises():
    """An unreachable Chrome (probe raises) reports cdp_alive: False, and the
    tool never raises — other keys are still present in the returned dict."""
    mock_session = mock.MagicMock(spec=CDPSession)
    mock_session.send.side_effect = TimeoutError("CDP 'Target.getTargetInfo' timed out after 2.0s")
    engine = _fake_engine(session=mock_session)

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["cdp_alive"] is False
    # The probe failure must be validate-only: the rest of the record keeps
    # the real values supplied by the fake engine/state, not blanked to None.
    assert info["session_id"] == "test-session"
    assert info["pid"] is None
    assert info["port"] == 9222
    assert info["user_data_dir"] == str(Path("/tmp/udd"))
    assert info["profile"] is None
    assert info["hwnd"] is None
    assert info["window_title"] is None


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        ConnectionError("connection reset"),
        OSError("socket error"),
        AssertionError("CDPSession.send() called before connect()"),
        TypeError("argument of type 'NoneType' is not iterable"),
        KeyError("code"),
        RuntimeError("torn down"),
    ],
)
def test_get_instance_info_cdp_alive_false_for_any_probe_exception(exc):
    """Any non-CDPError exception raised by the probe yields cdp_alive: False,
    never propagates out of get_instance_info(), for every raise path
    documented in the plan (TimeoutError, the assert in send(), whatever
    websocket-client raises, TypeError/KeyError from a malformed frame, and
    an arbitrary RuntimeError)."""
    mock_session = mock.MagicMock(spec=CDPSession)
    mock_session.send.side_effect = exc
    engine = _fake_engine(session=mock_session)

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["cdp_alive"] is False


def test_get_instance_info_cdp_alive_true_on_cdp_error():
    """A CDP protocol-level error (Chrome answered, just rejected the call)
    still counts as alive — pins that `except CDPError` is checked before the
    broader `except Exception` clause."""
    mock_session = mock.MagicMock(spec=CDPSession)
    mock_session.send.side_effect = CDPError("-32000: No such target")
    engine = _fake_engine(session=mock_session)

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["cdp_alive"] is True


def test_get_instance_info_cdp_alive_false_when_no_session():
    """No CDPSession attached (session is None) means dead, with no probe
    attempted and no exception raised."""
    engine = _fake_engine()  # default session=None

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(server_module, "load_state", return_value=None),
        mock.patch.object(server_module, "find_chrome_hwnd", return_value=(None, None)),
    ):
        info = get_instance_info()

    assert info["cdp_alive"] is False


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
                "chrome_wrapper_plugin.server.claim_session_slot",
                return_value=("eng-session", live_state),
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
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.claim_session_slot",
                return_value=("eng-session", None),
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
        assert saved.owner_pid == os.getpid()
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
                "chrome_wrapper_plugin.server.claim_session_slot",
                return_value=("eng-session", live_state),
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
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.claim_session_slot",
                return_value=("eng-session", None),
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
                "chrome_wrapper_plugin.server.claim_session_slot",
                return_value=("eng-session", live_state),
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
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.claim_session_slot",
                return_value=("eng-session", None),
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


# ── TestGetEngineOwnership ────────────────────────────────────────────────────
#
# Regression coverage for ticket #27: two wrapper processes that resolve the
# same base session id (e.g. both hitting the deterministic host+user
# fallback) must never reattach to each other's Chrome instance.  Uses a real
# tmp_path state dir (via CLAUDE_PLUGIN_DATA) and the real claim_session_slot
# — i.e. this class exercises the real ownership-claiming logic, unlike the
# other _get_engine tests above which mock claim_session_slot's return value.

class TestGetEngineOwnership:
    def setup_method(self):
        server_module._engine = None

    def teardown_method(self):
        server_module._engine = None

    def test_live_foreign_owner_forces_fresh_launch(self, tmp_path, monkeypatch):
        """A slot owned by a live, different wrapper process is skipped —
        this process claims the next slot and launches its own Chrome."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session")

        foreign_owner_pid = os.getppid()  # a real, live PID that isn't us
        save_state(
            _make_session_state(
                session_id="eng-session",
                pid=os.getpid(),
                port=9300,
                owner_pid=foreign_owner_pid,
            )
        )

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 24680

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.find_free_port", return_value=9401
            ),
            mock.patch("chrome_wrapper_plugin.server.seed_profile"),
            mock.patch(
                "chrome_wrapper_plugin.server.launch_chrome", return_value=fake_proc
            ) as mock_launch,
            mock.patch("chrome_wrapper_plugin.server.wait_for_cdp"),
            mock.patch("tempfile.mkdtemp", return_value=str(tmp_path / "udd")),
            mock.patch.object(CDPSession, "__init__", return_value=None),
            mock.patch.object(CDPSession, "connect", return_value=None),
            mock.patch("chrome_wrapper_plugin.server._attach_buffers"),
        ):
            engine = _get_engine()

        mock_launch.assert_called_once()
        assert engine.proc is not None
        assert engine.session_id == "eng-session-2"

    def test_launch_failure_removes_placeholder(self, tmp_path, monkeypatch):
        """A failure anywhere in the fresh-launch path must drop the claimed
        slot (the reservation placeholder) rather than leaving it stuck."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session-launch-fail")

        # Use a REAL mkdtemp (rooted under tmp_path) rather than a plain
        # return_value mock, so a real directory exists on disk that the
        # cleanup path either does or does not remove.
        real_mkdtemp = tempfile.mkdtemp
        created: list[str] = []

        def _real_mkdtemp_under_tmp_path(prefix=None, **kwargs):
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created.append(d)
            return d

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.find_free_port", return_value=9401
            ),
            mock.patch("chrome_wrapper_plugin.server.seed_profile"),
            mock.patch(
                "chrome_wrapper_plugin.server.launch_chrome",
                side_effect=RuntimeError("chrome.exe failed to start"),
            ),
            mock.patch("tempfile.mkdtemp", side_effect=_real_mkdtemp_under_tmp_path),
        ):
            with pytest.raises(RuntimeError, match="chrome.exe failed to start"):
                _get_engine()

        assert server_module._engine is None
        assert load_state("eng-session-launch-fail") is None
        assert not (tmp_path / "instances" / "eng-session-launch-fail.json").exists()
        assert len(created) == 1
        assert not Path(created[0]).exists(), (
            "user_data_dir must be removed even though launch_chrome() raised "
            "before `proc` was ever assigned (proc stays None the whole way "
            "through) — gating cleanup on `proc is not None` leaks this dir."
        )

    def test_seed_profile_failure_removes_user_data_dir(self, tmp_path, monkeypatch):
        """If seed_profile() raises (proc is never assigned at all — launch_chrome
        is never even called), the already-created user_data_dir must still be
        cleaned up.

        Pre-fix RED: the except-block in _get_engine() gated cleanup on
        `proc is not None`. Here proc is None the entire time (seed_profile
        raises before `launch_chrome()` runs), so terminate_chrome() was never
        called and the temp dir was left on disk — this assertion fails on the
        unfixed code.
        """
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "eng-session-seed-fail")

        real_mkdtemp = tempfile.mkdtemp
        created: list[str] = []

        def _real_mkdtemp_under_tmp_path(prefix=None, **kwargs):
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created.append(d)
            return d

        with (
            mock.patch("chrome_wrapper_plugin.server.reap_orphans"),
            mock.patch(
                "chrome_wrapper_plugin.server.find_free_port", return_value=9402
            ),
            mock.patch(
                "chrome_wrapper_plugin.server.seed_profile",
                side_effect=RuntimeError("seed copy failed"),
            ),
            mock.patch("chrome_wrapper_plugin.server.launch_chrome") as mock_launch,
            mock.patch("tempfile.mkdtemp", side_effect=_real_mkdtemp_under_tmp_path),
        ):
            with pytest.raises(RuntimeError, match="seed copy failed"):
                _get_engine()
            mock_launch.assert_not_called()

        assert server_module._engine is None
        assert load_state("eng-session-seed-fail") is None
        assert len(created) == 1
        assert not Path(created[0]).exists(), (
            "user_data_dir must be removed when seed_profile() raises before "
            "proc is ever assigned; gating cleanup on `proc is not None` leaks "
            "this dir."
        )


# ── lifespan / get_instance_info slot-awareness ───────────────────────────────

def test_lifespan_deletes_claimed_slot_state():
    """_lifespan teardown deletes the state file for the CLAIMED slot, not
    resolve_session_id()'s base id.  Regression pin: guards against a future
    change back to delete_state(resolve_session_id())."""
    import asyncio

    engine = _fake_engine(session_id="eng-session-2")
    engine.session = mock.MagicMock()
    server_module._engine = engine

    async def _run():
        async with server_module._lifespan(None):
            pass

    with (
        mock.patch("chrome_wrapper_plugin.server.terminate_chrome"),
        mock.patch("chrome_wrapper_plugin.server.delete_state") as mock_delete,
        mock.patch(
            "chrome_wrapper_plugin.server.resolve_session_id",
            return_value="eng-session",
        ),
    ):
        asyncio.run(_run())

    mock_delete.assert_called_once_with("eng-session-2")
    assert server_module._engine is None


def test_get_instance_info_uses_slot_session_id():
    """get_instance_info()'s load_state(engine.session_id) resolves the
    CLAIMED (possibly suffixed) slot, not the unsuffixed base id."""
    engine = _fake_engine(proc=None, port=9333, session_id="eng-session-2")
    saved = _make_session_state(session_id="eng-session-2", pid=77777)

    with (
        mock.patch.object(server_module, "_get_engine", return_value=engine),
        mock.patch.object(
            server_module, "load_state", return_value=saved
        ) as mock_load,
        mock.patch.object(
            server_module, "find_chrome_hwnd", return_value=(None, None)
        ) as mock_hwnd,
    ):
        get_instance_info()

    mock_load.assert_called_once_with("eng-session-2")
    mock_hwnd.assert_called_once_with(77777)
