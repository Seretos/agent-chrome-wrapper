from __future__ import annotations

import dataclasses
import datetime
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from chrome_wrapper_plugin.chrome_process import (
    find_free_port,
    launch_chrome,
    wait_for_cdp,
)
from chrome_wrapper_plugin.profiles import master_profile_dir, seed_profile
from chrome_wrapper_plugin.state import (
    SessionState,
    is_process_alive,
    load_state,
    reap_orphans,
    resolve_session_id,
    save_state,
)
# TODO(#5): import delete_state + terminate_chrome and wire them into
# shutdown/lifespan teardown so ephemeral user-data-dirs are cleaned up
# and the session state file is removed on clean exit.

mcp = FastMCP("chrome-wrapper")


@dataclasses.dataclass
class ChromeEngine:
    """Holds all runtime state for this session's Chrome instance."""

    proc: Optional[subprocess.Popen]  # None when reattached (no Popen handle)
    port: int
    user_data_dir: Path
    session_id: str


_engine: Optional[ChromeEngine] = None


def _get_engine() -> ChromeEngine:
    """Return (lazily creating) the Chrome engine for this MCP session."""
    global _engine
    if _engine is not None:
        return _engine

    session_id = resolve_session_id()

    # Reap dead sessions so they don't accumulate temp directories
    reap_orphans()

    # Try to reattach to an already-running Chrome (crash-recovery path)
    state = load_state(session_id)
    if state is not None and is_process_alive(state.pid):
        _engine = ChromeEngine(
            proc=None,
            port=state.port,
            user_data_dir=Path(state.user_data_dir),
            session_id=session_id,
        )
        return _engine

    # Fresh launch
    port = find_free_port()
    # Each session gets its own ephemeral user-data-dir under the system temp
    user_data_dir = Path(tempfile.mkdtemp(prefix=f"chrome_wrapper_{session_id}_"))

    # Seed from master profile (cookies/logins) — skips gracefully if absent
    seed_profile(master_profile_dir(), user_data_dir)

    proc = launch_chrome(user_data_dir, port)
    wait_for_cdp(port)

    now = datetime.datetime.now().isoformat()
    save_state(
        SessionState(
            session_id=session_id,
            pid=proc.pid,
            port=port,
            user_data_dir=str(user_data_dir),
            profile=str(master_profile_dir()),
            created_at=now,
        )
    )

    _engine = ChromeEngine(
        proc=proc,
        port=port,
        user_data_dir=user_data_dir,
        session_id=session_id,
    )
    return _engine


@mcp.tool()
def ping() -> str:
    """Health check tool. Replace with real tools as you build them out."""
    return "pong"


@mcp.tool()
def get_instance_info() -> dict:
    """Return information about the current Chrome instance for this session.

    Triggers a lazy Chrome launch if none is running yet.

    Keys:
    - ``session_id``: stable ID for this MCP session.
    - ``pid``: Chrome process PID (None when reattached without a Popen handle).
    - ``port``: CDP remote-debugging port.
    - ``user_data_dir``: path to the ephemeral user-data directory.
    - ``hwnd``: Chrome top-level window handle — deferred to ticket #5.
    """
    engine = _get_engine()
    pid = engine.proc.pid if engine.proc is not None else None
    return {
        "session_id": engine.session_id,
        "pid": pid,
        "port": engine.port,
        "user_data_dir": str(engine.user_data_dir),
        "hwnd": None,  # populated in ticket #5
    }


def main() -> None:
    mcp.run()
