from __future__ import annotations

import base64
import dataclasses
import datetime
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

from chrome_wrapper_plugin.cdp import CDPSession
from chrome_wrapper_plugin.chrome_process import (
    find_free_port,
    launch_chrome,
    wait_for_cdp,
)
from chrome_wrapper_plugin.hwnd import find_chrome_hwnd
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
    session: Optional[CDPSession] = dataclasses.field(default=None)


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
        # wait_for_cdp guards against a Chrome that is alive-by-PID but not
        # yet accepting CDP connections (e.g. still starting up after a crash).
        wait_for_cdp(state.port)
        engine = ChromeEngine(
            proc=None,
            port=state.port,
            user_data_dir=Path(state.user_data_dir),
            session_id=session_id,
        )
        engine.session = CDPSession(port=state.port)
        engine.session.connect()   # raises → _engine stays None (no cache poison)
        _engine = engine
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

    engine = ChromeEngine(
        proc=proc,
        port=port,
        user_data_dir=user_data_dir,
        session_id=session_id,
    )
    engine.session = CDPSession(port=port)
    engine.session.connect()   # raises → _engine stays None (no cache poison)
    _engine = engine
    return _engine


@mcp.tool()
def navigate(url: str, wait_until: str = "load") -> dict:
    """Navigate the current Chrome page to *url* and wait for it to load.

    Parameters
    ----------
    url:
        The URL to navigate to.
    wait_until:
        Wait condition.  Only ``"load"`` is supported for MVP; any other value
        raises ``ValueError``.

    Returns
    -------
    dict
        The raw ``Page.navigate`` CDP result dict (contains ``frameId``,
        ``loaderId``, and optionally ``errorText``).
    """
    if wait_until != "load":
        raise ValueError(
            f"wait_until={wait_until!r} is not supported; only 'load' is valid for MVP"
        )

    engine = _get_engine()
    session = engine.session

    load_event = threading.Event()

    def _on_load(params: dict) -> None:  # noqa: ARG001
        load_event.set()

    session.add_listener("Page.loadEventFired", _on_load)
    try:
        result = session.send("Page.navigate", {"url": url})
        fired = load_event.wait(timeout=30.0)
        if not fired:
            raise TimeoutError(
                f"Page.loadEventFired not received within 30s after navigating to {url!r}"
            )
        return result
    finally:
        session.remove_listener("Page.loadEventFired", _on_load)


@mcp.tool()
def get_page_info() -> dict:
    """Return URL and title of the page currently loaded in Chrome.

    Returns
    -------
    dict
        A dict with at least ``url`` and ``title`` keys, sourced from
        ``Target.getTargetInfo``.
    """
    engine = _get_engine()
    result = engine.session.send("Target.getTargetInfo", {})
    target_info = result["targetInfo"]
    return {
        "url": target_info["url"],
        "title": target_info["title"],
    }


@mcp.tool()
def screenshot(full_page: bool = False) -> Image:
    """Capture a PNG screenshot of the current Chrome viewport.

    Parameters
    ----------
    full_page:
        Accepted for API consistency but ignored for MVP — only the
        viewport is captured.

    Returns
    -------
    Image
        A FastMCP ``Image`` object containing the PNG bytes.
    """
    # TODO(#3): wire captureBeyondViewport when full_page is True
    engine = _get_engine()
    result = engine.session.send("Page.captureScreenshot", {"format": "png"})
    png_bytes = base64.b64decode(result["data"])
    return Image(data=png_bytes, format="png")


@mcp.tool()
def evaluate_js(expression: str) -> dict:
    """Evaluate *expression* in the page's JavaScript context and return the result.

    Parameters
    ----------
    expression:
        A JavaScript expression or statement to evaluate.  Promises are
        awaited automatically.

    Returns
    -------
    dict
        The raw ``Runtime.evaluate`` CDP result dict (contains a ``result``
        sub-dict with ``type``, ``value``, etc.).
    """
    engine = _get_engine()
    return engine.session.send(
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
    )


@mcp.tool()
def cdp(method: str, params: dict) -> dict:
    """Send a raw CDP command and return its result dict.

    This is the architectural completeness guarantee — use it whenever a
    high-level tool is missing.  The agent is never blocked.

    Parameters
    ----------
    method:
        CDP method, e.g. ``"DOM.getDocument"``.
    params:
        Parameters dict to pass to the CDP method.

    Returns
    -------
    dict
        The raw CDP result dict.
    """
    engine = _get_engine()
    return engine.session.send(method, params)


@mcp.tool()
def get_instance_info() -> dict:
    """Return information about the current Chrome instance for this session.

    Triggers a lazy Chrome launch if none is running yet.

    Keys
    ----
    session_id : str
        Stable ID for this MCP session.
    pid : int | None
        Chrome process PID; None when reattached without a Popen handle.
    port : int
        CDP remote-debugging port.
    user_data_dir : str
        Path to the ephemeral user-data directory.
    profile : str | None
        Master profile path used to seed this session; None if unavailable.
    hwnd : int | None
        Chrome top-level window handle (HWND).  Pass to vdesktop.adopt_window(hwnd).
        None on non-Windows or when the window is not yet visible.
    window_title : str | None
        Title of the Chrome browser-frame window, or None when hwnd is None.
    """
    engine = _get_engine()
    pid = engine.proc.pid if engine.proc is not None else None

    saved = load_state(engine.session_id)
    profile = saved.profile if saved is not None else None
    # On the reattach path (proc is None), recover pid from saved state for HWND lookup
    lookup_pid = pid if pid is not None else (saved.pid if saved is not None else None)

    hwnd, window_title = (
        find_chrome_hwnd(lookup_pid) if lookup_pid is not None else (None, None)
    )

    return {
        "session_id": engine.session_id,
        "pid": pid,
        "port": engine.port,
        "user_data_dir": str(engine.user_data_dir),
        "profile": profile,
        "hwnd": hwnd,
        "window_title": window_title,
    }


def main() -> None:
    mcp.run()
