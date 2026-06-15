"""Tests for chrome_wrapper_plugin.chrome_process.

All tests are mock-only: no real Chrome, no real registry, no real ports.
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from chrome_wrapper_plugin.chrome_process import (
    find_chrome,
    find_free_port,
    launch_chrome,
    terminate_chrome,
    wait_for_cdp,
)


# ── find_chrome ──────────────────────────────────────────────────────────────

def test_find_chrome_env_override(tmp_path, monkeypatch):
    fake_exe = tmp_path / "chrome.exe"
    fake_exe.touch()
    monkeypatch.setenv("CHROME_WRAPPER_CHROME_PATH", str(fake_exe))
    assert find_chrome() == fake_exe


def test_find_chrome_registry_hklm(monkeypatch, tmp_path):
    """HKLM App Paths registry key is consulted when env-var is absent."""
    winreg = pytest.importorskip("winreg")

    fake_exe = tmp_path / "chrome.exe"
    fake_exe.touch()
    monkeypatch.delenv("CHROME_WRAPPER_CHROME_PATH", raising=False)

    def fake_open_key(hive, key):
        if hive == winreg.HKEY_LOCAL_MACHINE:
            return mock.MagicMock()
        raise OSError("not found")

    def fake_query(key, name):
        return (str(fake_exe), 1)

    with (
        mock.patch("winreg.OpenKey", side_effect=fake_open_key),
        mock.patch("winreg.QueryValueEx", side_effect=fake_query),
    ):
        result = find_chrome()

    assert result == fake_exe


def test_find_chrome_registry_hkcu_fallback(monkeypatch, tmp_path):
    """HKCU App Paths is tried when HKLM key is absent."""
    winreg = pytest.importorskip("winreg")

    fake_exe = tmp_path / "chrome.exe"
    fake_exe.touch()
    monkeypatch.delenv("CHROME_WRAPPER_CHROME_PATH", raising=False)

    def fake_open_key(hive, key):
        if hive == winreg.HKEY_LOCAL_MACHINE:
            raise OSError("not found")
        return mock.MagicMock()

    def fake_query(key, name):
        return (str(fake_exe), 1)

    with (
        mock.patch("winreg.OpenKey", side_effect=fake_open_key),
        mock.patch("winreg.QueryValueEx", side_effect=fake_query),
    ):
        result = find_chrome()

    assert result == fake_exe


def test_find_chrome_filesystem_fallback(monkeypatch, tmp_path):
    """ProgramFiles fallback is used when registry is absent."""
    monkeypatch.delenv("CHROME_WRAPPER_CHROME_PATH", raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LocalAppData", raising=False)

    fake_exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.touch()

    # Stub out winreg so the registry path is skipped
    with mock.patch.dict("sys.modules", {"winreg": None}):
        result = find_chrome()

    assert result == fake_exe


def test_find_chrome_raises_when_nothing_found(monkeypatch, tmp_path):
    """FileNotFoundError is raised when no Chrome location resolves."""
    monkeypatch.delenv("CHROME_WRAPPER_CHROME_PATH", raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LocalAppData", raising=False)

    with mock.patch.dict("sys.modules", {"winreg": None}):
        with pytest.raises(FileNotFoundError, match="Chrome executable not found"):
            find_chrome()


# ── find_free_port ───────────────────────────────────────────────────────────

def test_find_free_port_returns_int_in_range():
    port = find_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


# ── wait_for_cdp ─────────────────────────────────────────────────────────────

def test_wait_for_cdp_succeeds_on_first_poll(monkeypatch):
    fake_resp = mock.MagicMock()
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = mock.MagicMock(return_value=False)
    fake_resp.status = 200

    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        wait_for_cdp(9222, timeout=5.0)  # should not raise


def test_wait_for_cdp_retries_then_succeeds(monkeypatch):
    """URLError on first attempt, success on second."""
    call_count = 0

    def fake_urlopen(url, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise urllib.error.URLError("not ready")
        resp = mock.MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = mock.MagicMock(return_value=False)
        resp.status = 200
        return resp

    with (
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        mock.patch("time.sleep"),
    ):
        wait_for_cdp(9222, timeout=5.0)

    assert call_count == 2


def test_wait_for_cdp_times_out(monkeypatch):
    """TimeoutError is raised when Chrome never responds."""

    with (
        mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ),
        mock.patch("time.sleep"),
        # Make monotonic() advance fast so the timeout loop exits quickly
        mock.patch("time.monotonic", side_effect=[0.0, 0.0, 100.0]),
    ):
        with pytest.raises(TimeoutError):
            wait_for_cdp(9222, timeout=1.0)


# ── terminate_chrome ─────────────────────────────────────────────────────────

def test_terminate_chrome_already_exited(tmp_path):
    """terminate_chrome is safe when proc has already exited."""
    proc = mock.MagicMock(spec=subprocess.Popen)
    proc.terminate = mock.MagicMock()
    proc.wait = mock.MagicMock()
    proc.kill = mock.MagicMock()

    # Simulate clean exit
    proc.wait.return_value = 0

    user_data_dir = tmp_path / "session_udd"
    user_data_dir.mkdir()
    (user_data_dir / "dummy.txt").write_text("x")

    terminate_chrome(proc, user_data_dir)

    proc.terminate.assert_called_once()
    proc.wait.assert_called_once()
    assert not user_data_dir.exists()


def test_terminate_chrome_none_proc(tmp_path):
    """terminate_chrome(None, ...) just removes the directory."""
    user_data_dir = tmp_path / "session_udd"
    user_data_dir.mkdir()

    terminate_chrome(None, user_data_dir)

    assert not user_data_dir.exists()
