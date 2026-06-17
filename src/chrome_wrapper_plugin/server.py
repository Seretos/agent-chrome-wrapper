from __future__ import annotations

import base64
import collections
import dataclasses
import datetime
import logging
import subprocess
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

from chrome_wrapper_plugin.cdp import CDPSession
from chrome_wrapper_plugin.chrome_process import (
    find_free_port,
    launch_chrome,
    terminate_chrome,
    wait_for_cdp,
)
from chrome_wrapper_plugin.hwnd import find_chrome_hwnd
from chrome_wrapper_plugin.profiles import master_profile_dir, seed_profile
from chrome_wrapper_plugin.state import (
    SessionState,
    delete_state,
    is_process_alive,
    load_state,
    reap_orphans,
    resolve_session_id,
    save_state,
)

@asynccontextmanager
async def _lifespan(server):  # noqa: ARG001
    """Clean up Chrome and session state on MCP server shutdown."""
    yield
    # Teardown runs after the yield — mirrors FastMCP's lifespan contract.
    global _engine
    if _engine is not None:
        engine = _engine
        _engine = None  # clear before teardown so a racing call sees None
        try:
            engine.session.close()
        except Exception:
            pass
        terminate_chrome(engine.proc, engine.user_data_dir)
        delete_state(engine.session_id)


mcp = FastMCP("chrome-wrapper", lifespan=_lifespan)


@dataclasses.dataclass
class ChromeEngine:
    """Holds all runtime state for this session's Chrome instance."""

    proc: Optional[subprocess.Popen]  # None when reattached (no Popen handle)
    port: int
    user_data_dir: Path
    session_id: str
    session: Optional[CDPSession] = dataclasses.field(default=None)
    # Ring buffers populated by CDP-event listeners; drained (cleared) on each tool call.
    console_buffer: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=500)
    )
    network_buffer: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=500)
    )
    # Side-map populated by Network.requestWillBeSent on the WS thread.
    # Keyed by requestId (str); used to populate the `url` field of
    # Network.loadingFailed events, which carry no url of their own.
    # Entries are pruned on loadingFailed and responseReceived to bound size.
    request_url_map: dict = dataclasses.field(default_factory=dict)


_engine: Optional[ChromeEngine] = None


def _attach_buffers(engine: ChromeEngine) -> None:
    """Register CDP-event listeners that populate the console and network ring buffers.

    Called immediately after engine.session.connect() on both the reattach path
    and the fresh-launch path.  Each callback receives the CDP params dict that
    CDPSession dispatches, and appends a normalised entry to the appropriate buffer.
    The buffers are drained (cleared) when the corresponding tool is called.
    """
    session = engine.session

    def _on_console_api(params: dict) -> None:
        # CDP sends the severity as "type" (e.g. "log", "warning", "error");
        # we store it under "level" to match the normalised entry shape.
        engine.console_buffer.append(
            {
                "type": "consoleAPI",
                "level": params.get("type"),
                "args": params.get("args"),
                "timestamp": params.get("timestamp"),
            }
        )

    def _on_exception_thrown(params: dict) -> None:
        detail = params.get("exceptionDetails", {})
        engine.console_buffer.append(
            {
                "type": "exception",
                "text": detail.get("text"),
                "exception": detail.get("exception"),
                "url": detail.get("url"),
                "lineNumber": detail.get("lineNumber"),
                "timestamp": params.get("timestamp"),
            }
        )

    def _on_log_entry(params: dict) -> None:
        entry = params.get("entry", {})
        engine.console_buffer.append(
            {
                "type": "log",
                "level": entry.get("level"),
                "text": entry.get("text"),
                "source": entry.get("source"),
                "url": entry.get("url"),
                "timestamp": entry.get("timestamp"),
            }
        )

    def _on_request_will_be_sent(params: dict) -> None:
        # Maintain a requestId → url side-map so that loadingFailed events
        # (which carry no url) can still report the originating URL.
        request_id = params.get("requestId")
        url = (params.get("request") or {}).get("url")
        if request_id is not None and url is not None:
            engine.request_url_map[request_id] = url

    def _on_response_received(params: dict) -> None:
        response = params.get("response", {})
        request_id = params.get("requestId")
        # Prune the side-map entry now that the request is settled.
        engine.request_url_map.pop(request_id, None)
        engine.network_buffer.append(
            {
                "event": "responseReceived",
                "requestId": request_id,
                "url": response.get("url"),
                "status": response.get("status"),
                "mimeType": response.get("mimeType"),
                "timing": response.get("timing"),
                "timestamp": params.get("timestamp"),
            }
        )

    def _on_loading_failed(params: dict) -> None:
        # Network.loadingFailed has no url/documentURL field in its top-level
        # params.  We look up the originating URL from the requestWillBeSent
        # side-map and prune that entry now that the request is settled.
        request_id = params.get("requestId")
        url = engine.request_url_map.pop(request_id, None)
        engine.network_buffer.append(
            {
                "event": "loadingFailed",
                "requestId": request_id,
                "url": url,
                "errorText": params.get("errorText"),
                "canceled": params.get("canceled"),
                "timestamp": params.get("timestamp"),
            }
        )

    session.add_listener("Runtime.consoleAPICalled", _on_console_api)
    session.add_listener("Runtime.exceptionThrown", _on_exception_thrown)
    session.add_listener("Log.entryAdded", _on_log_entry)
    session.add_listener("Network.requestWillBeSent", _on_request_will_be_sent)
    session.add_listener("Network.responseReceived", _on_response_received)
    session.add_listener("Network.loadingFailed", _on_loading_failed)


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
        _attach_buffers(engine)
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
    try:
        engine.session.connect()
    except Exception:
        terminate_chrome(proc, user_data_dir)
        delete_state(session_id)
        raise
    _attach_buffers(engine)
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


@mcp.tool()
def get_console_logs() -> list:
    """Return and drain all buffered browser console entries since the last call.

    Collects entries from three CDP event streams:

    - **consoleAPI** (``Runtime.consoleAPICalled``): explicit ``console.log/warn/error``
      etc. calls from page JavaScript.  Each entry has keys ``type`` (``"consoleAPI"``),
      ``level`` (e.g. ``"log"``, ``"warning"``, ``"error"``), ``args`` (list of remote
      objects), and ``timestamp``.

    - **exception** (``Runtime.exceptionThrown``): uncaught JavaScript exceptions.
      Each entry has keys ``type`` (``"exception"``), ``text``, ``exception``,
      ``url``, ``lineNumber``, and ``timestamp``.

    - **log** (``Log.entryAdded``): Chrome's browser-level log, which includes
      security warnings, deprecations, and network-level errors that do not surface
      through the Runtime domain.  Each entry has keys ``type`` (``"log"``),
      ``level``, ``text``, ``source``, ``url``, and ``timestamp``.

    The buffer holds at most 500 entries (oldest are dropped when full).
    Calling this tool clears the buffer so each call returns only new entries.

    Returns
    -------
    list
        A list of entry dicts (may be empty if nothing was logged).
    """
    engine = _get_engine()
    # Atomic swap: replace the buffer with a fresh deque before iterating so
    # items appended by the WS thread between list() and clear() are not lost.
    # The callbacks close over `engine` and read engine.console_buffer on each
    # invocation, so they will append to the new deque going forward.
    old = engine.console_buffer
    engine.console_buffer = collections.deque(maxlen=500)
    return list(old)


@mcp.tool()
def get_network_log() -> list:
    """Return and drain all buffered network events since the last call.

    Collects entries from two CDP event streams:

    - **responseReceived** (``Network.responseReceived``): a response was received
      for a network request.  Each entry has keys ``event`` (``"responseReceived"``),
      ``requestId``, ``url``, ``status`` (HTTP status code), ``mimeType``,
      ``timing``, and ``timestamp``.

    - **loadingFailed** (``Network.loadingFailed``): a network request failed to
      load (DNS error, blocked by CSP, cancelled, etc.).  Each entry has keys
      ``event`` (``"loadingFailed"``), ``requestId``, ``url``, ``errorText``,
      ``canceled`` (bool or None), and ``timestamp``.

    The buffer holds at most 500 entries (oldest are dropped when full).
    Calling this tool clears the buffer so each call returns only new entries.

    Returns
    -------
    list
        A list of event dicts (may be empty if no network activity was captured).
    """
    engine = _get_engine()
    # Atomic swap: replace the buffer with a fresh deque before iterating so
    # items appended by the WS thread between list() and clear() are not lost.
    # The callbacks close over `engine` and read engine.network_buffer on each
    # invocation, so they will append to the new deque going forward.
    old = engine.network_buffer
    engine.network_buffer = collections.deque(maxlen=500)
    return list(old)


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mcp.run()
