# Session ID resolution order:
# 1. CLAUDE_SESSION_ID   — set by Claude Code host
# 2. ANTHROPIC_SESSION_ID — alternative host env-var
# 3. MCP_SESSION_ID      — generic MCP host env-var
# 4. Deterministic fallback: sha256(hostname + ":" + username).hexdigest()[:16]
#    Stable across restarts on the same host+user combination.

from __future__ import annotations

import ctypes
import ctypes.wintypes
import dataclasses
import datetime
import getpass
import hashlib
import json
import logging
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_session_id() -> str:
    """Return a stable session identifier for this process."""
    for var in ("CLAUDE_SESSION_ID", "ANTHROPIC_SESSION_ID", "MCP_SESSION_ID"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    # Deterministic fallback — stable across restarts on same host+user
    key = socket.gethostname() + ":" + getpass.getuser()
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _state_dir() -> Path:
    """Return (and create) the directory where session JSON files are stored."""
    base_env = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if base_env:
        base = Path(base_env)
    else:
        base = Path(tempfile.gettempdir()) / "chrome_wrapper_plugin"
    state_path = base / "instances"
    state_path.mkdir(parents=True, exist_ok=True)
    return state_path


@dataclasses.dataclass
class SessionState:
    session_id: str
    pid: int
    port: int
    user_data_dir: str  # stored as str for JSON round-trip
    profile: str
    created_at: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "SessionState":
        data = json.loads(raw)
        return cls(**data)


def save_state(state: SessionState) -> None:
    """Atomically write state JSON to <_state_dir()>/<session_id>.json."""
    target = _state_dir() / f"{state.session_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(state.to_json(), encoding="utf-8")
    os.replace(tmp, target)


def load_state(session_id: str) -> Optional[SessionState]:
    """Return SessionState for *session_id*, or None if missing or corrupt."""
    path = _state_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return SessionState.from_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("Corrupt session state at %s: %s", path, exc)
        return None


# ── process-liveness check (ctypes WinAPI, no psutil) ──────────────────────

PROCESS_QUERY_INFORMATION = 0x0400
_ERROR_ACCESS_DENIED = 5  # GetLastError() value when process exists but is inaccessible


def is_process_alive(pid: int) -> bool:
    """Return True when *pid* refers to a running process on Windows.

    OpenProcess returns a null handle both when the PID does not exist
    (ERROR_INVALID_PARAMETER / 87) AND when the process exists but the
    caller lacks permission to open it (ERROR_ACCESS_DENIED / 5).  We
    must treat ACCESS_DENIED as *alive* to avoid reaping a live session
    owned by a more-privileged user or a protected system process.
    """
    try:
        _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except AttributeError:
        # Non-Windows (e.g. Linux CI): fall back to POSIX signal-0 check.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # Set correct return/arg types so the handle value is never truncated on
    # 64-bit Windows (HANDLE is pointer-sized; default ctypes restype is c_int).
    _kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,   # dwDesiredAccess
        ctypes.wintypes.BOOL,    # bInheritHandle
        ctypes.wintypes.DWORD,   # dwProcessId
    ]
    _kernel32.GetLastError.restype = ctypes.wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

    handle = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    # Null handle: ERROR_ACCESS_DENIED (5) means the process exists but we
    # cannot open it — still alive.
    return _kernel32.GetLastError() == _ERROR_ACCESS_DENIED


def reap_orphans() -> list[str]:
    """Delete state files (and user-data-dirs) for dead Chrome processes.

    Returns the list of session IDs that were reaped.
    """
    reaped: list[str] = []
    for json_path in _state_dir().glob("*.json"):
        try:
            state = SessionState.from_json(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Skipping corrupt state file %s: %s", json_path, exc)
            continue

        if not is_process_alive(state.pid):
            logger.debug("Reaping orphan session %s (pid %d)", state.session_id, state.pid)
            shutil.rmtree(state.user_data_dir, ignore_errors=True)
            try:
                json_path.unlink()
            except OSError:
                pass
            reaped.append(state.session_id)

    return reaped


def delete_state(session_id: str) -> None:
    """Remove the state JSON for *session_id* if it exists."""
    path = _state_dir() / f"{session_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
