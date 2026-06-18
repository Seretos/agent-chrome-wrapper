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
import time
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
def wait_for_selector(
    selector: str,
    timeout: float = 30.0,
    state: str = "visible",
) -> dict:
    """Wait until a DOM element matching *selector* reaches *state*.

    Parameters
    ----------
    selector:
        A CSS selector string.
    timeout:
        Maximum seconds to wait (default 30).
    state:
        ``"attached"`` — element exists in the DOM regardless of visibility.
        ``"visible"``  — element exists and is not hidden (not
        ``display:none``, not ``visibility:hidden``, non-zero bounding rect).

    Returns
    -------
    dict
        ``{"selector": selector, "state": state, "elapsed": float}`` on
        success.

    Raises
    ------
    ValueError
        If *state* is not ``"attached"`` or ``"visible"``.
    TimeoutError
        If the element does not reach *state* within *timeout* seconds.
    RuntimeError
        If the Runtime.evaluate call returns exceptionDetails.
    """
    if state not in ("attached", "visible"):
        raise ValueError(
            f"state={state!r} is not supported; use 'attached' or 'visible'"
        )

    if state == "attached":
        expr = (
            f"(function(){{"
            f"  var el = document.querySelector({selector!r});"
            f"  return el !== null;"
            f"}})();"
        )
    else:  # visible
        expr = (
            f"(function(){{"
            f"  var el = document.querySelector({selector!r});"
            f"  if (!el) return false;"
            f"  var s = window.getComputedStyle(el);"
            f"  if (s.display === 'none' || s.visibility === 'hidden') return false;"
            f"  var r = el.getBoundingClientRect();"
            f"  return r.width > 0 && r.height > 0;"
            f"}})();"
        )

    engine = _get_engine()
    session = engine.session
    start = time.monotonic()
    deadline = start + timeout
    interval = 0.1

    while True:
        result = session.send(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": False, "returnByValue": True},
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(
                f"JS exception in wait_for_selector({selector!r}): "
                f"{result['exceptionDetails']}"
            )
        if result.get("result", {}).get("value") is True:
            return {
                "selector": selector,
                "state": state,
                "elapsed": round(time.monotonic() - start, 3),
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"wait_for_selector({selector!r}, state={state!r}) "
                f"timed out after {timeout}s"
            )
        time.sleep(min(interval, remaining))


@mcp.tool()
def wait_for_navigation(timeout: float = 30.0) -> dict:
    """Wait for the next page load event to fire (Page.loadEventFired).

    Trigger navigation first (e.g. call ``click()`` on a link), then call
    this tool to wait for the resulting page load to complete.

    This tool waits for the NEXT ``Page.loadEventFired`` event after the
    listener is registered.  If the page load completes before this tool is
    called, the event will already have fired and this tool will wait for a
    subsequent load or time out.  In a synchronous MCP server the caller
    cannot issue the navigation-triggering action while this tool is
    blocking, so pre-registration of the listener is not possible — trigger
    the navigation action first, then call this tool.

    Parameters
    ----------
    timeout:
        Maximum seconds to wait (default 30).

    Returns
    -------
    dict
        ``{"event": "Page.loadEventFired"}`` on success.

    Raises
    ------
    TimeoutError
        If no load event fires within *timeout* seconds.
    """
    engine = _get_engine()
    session = engine.session

    load_event = threading.Event()

    def _on_load(params: dict) -> None:  # noqa: ARG001
        load_event.set()

    session.add_listener("Page.loadEventFired", _on_load)
    try:
        fired = load_event.wait(timeout=timeout)
        if not fired:
            raise TimeoutError(
                f"Page.loadEventFired not received within {timeout}s"
            )
        return {"event": "Page.loadEventFired"}
    finally:
        session.remove_listener("Page.loadEventFired", _on_load)


@mcp.tool()
def wait_for_network_idle(timeout: float = 30.0) -> dict:
    """Wait until all in-flight network requests have completed.

    Registers its own Network-domain listeners (independent of the ring-buffer
    listeners attached by the engine at startup) to count in-flight requests.
    Resolves when the counter reaches zero, or immediately if there are no
    in-flight requests at call time.

    Parameters
    ----------
    timeout:
        Maximum seconds to wait (default 30).

    Returns
    -------
    dict
        ``{"event": "networkIdle"}`` on success.

    Raises
    ------
    TimeoutError
        If the network does not go idle within *timeout* seconds.
    """
    engine = _get_engine()
    session = engine.session

    idle_event = threading.Event()
    _lock = threading.Lock()
    _in_flight: list[int] = [0]  # mutable cell so closures can mutate it

    def _on_request(params: dict) -> None:  # noqa: ARG001
        with _lock:
            _in_flight[0] += 1
            idle_event.clear()

    def _on_done(params: dict) -> None:  # noqa: ARG001
        with _lock:
            _in_flight[0] = max(0, _in_flight[0] - 1)
            if _in_flight[0] == 0:
                idle_event.set()

    session.add_listener("Network.requestWillBeSent", _on_request)
    session.add_listener("Network.loadingFinished", _on_done)
    session.add_listener("Network.loadingFailed", _on_done)
    # Pre-set only when no requests are already in flight, so that a request
    # that arrived via the WS daemon thread during/after listener registration
    # (which increments _in_flight) is not erroneously reported as idle.
    with _lock:
        if _in_flight[0] == 0:
            idle_event.set()
    try:
        fired = idle_event.wait(timeout=timeout)
        if not fired:
            raise TimeoutError(
                f"Network did not go idle within {timeout}s "
                f"({_in_flight[0]} request(s) still in flight)"
            )
        return {"event": "networkIdle"}
    finally:
        session.remove_listener("Network.requestWillBeSent", _on_request)
        session.remove_listener("Network.loadingFinished", _on_done)
        session.remove_listener("Network.loadingFailed", _on_done)


@mcp.tool()
def sleep(seconds: float) -> dict:
    """Pause execution for *seconds* seconds.

    Parameters
    ----------
    seconds:
        Duration to sleep.  Must be between 0 and 60 inclusive.

    Returns
    -------
    dict
        ``{"slept": seconds}``.

    Raises
    ------
    ValueError
        If *seconds* is negative or exceeds 60.
    """
    if seconds < 0 or seconds > 60:
        raise ValueError(
            f"seconds must be between 0 and 60 inclusive, got {seconds!r}"
        )
    time.sleep(seconds)
    return {"slept": seconds}


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


def _resolve_element_center(session: CDPSession, selector: str) -> tuple[float, float]:
    """Resolve *selector* to viewport center coordinates (x, y).

    Uses a single Runtime.evaluate round-trip: queries the element, scrolls it
    into view, and returns the center of its bounding rect.

    Raises
    ------
    ValueError
        If no element matches *selector*.
    RuntimeError
        If the Runtime.evaluate call returns exceptionDetails.
    """
    expr = (
        f"(function(){{"
        f"  var el = document.querySelector({selector!r});"
        f"  if (!el) return null;"
        f"  el.scrollIntoView({{block:'center',inline:'center'}});"
        f"  var r = el.getBoundingClientRect();"
        f"  return {{x: r.left + r.width/2, y: r.top + r.height/2}};"
        f"}})();"
    )
    result = session.send(
        "Runtime.evaluate",
        {"expression": expr, "awaitPromise": False, "returnByValue": True},
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(
            f"JS exception resolving selector {selector!r}: "
            f"{result['exceptionDetails']}"
        )
    value = result.get("result", {}).get("value")
    if value is None:
        raise ValueError(f"No element found for selector {selector!r}")
    return float(value["x"]), float(value["y"])


@mcp.tool()
def click(selector: str) -> dict:
    """Click the first element matching *selector* using trusted mouse events.

    Scrolls the element into view, moves the mouse to its center, then
    dispatches mousePressed and mouseReleased via ``Input.dispatchMouseEvent``
    so that ``event.isTrusted === true`` inside the page.

    Parameters
    ----------
    selector:
        A CSS selector string, e.g. ``"#submit-btn"`` or ``"button.primary"``.

    Returns
    -------
    dict
        ``{"x": float, "y": float}`` — the viewport coordinates of the click.

    Raises
    ------
    ValueError
        If no element matches *selector*.
    """
    engine = _get_engine()
    session = engine.session
    x, y = _resolve_element_center(session, selector)
    session.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": "none",
    })
    session.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": "left", "clickCount": 1,
    })
    session.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": "left", "clickCount": 1,
    })
    return {"x": x, "y": y}


@mcp.tool()
def hover(selector: str) -> dict:
    """Move the mouse to the center of the first element matching *selector*.

    Dispatches a trusted ``mousemove`` event via ``Input.dispatchMouseEvent``,
    which activates CSS ``:hover`` states and triggers ``mouseenter``/
    ``mouseover`` handlers.

    Parameters
    ----------
    selector:
        A CSS selector string.

    Returns
    -------
    dict
        ``{"x": float, "y": float}`` — the viewport coordinates of the hover.

    Raises
    ------
    ValueError
        If no element matches *selector*.
    """
    engine = _get_engine()
    session = engine.session
    x, y = _resolve_element_center(session, selector)
    session.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": "none",
    })
    return {"x": x, "y": y}


@mcp.tool()
def type(selector: str, text: str) -> dict:
    """Focus *selector* and type *text* using trusted keyboard events.

    Clicks the element first to give it focus, then dispatches
    ``Input.dispatchKeyEvent`` keyDown + keyUp for each character so that
    ``event.isTrusted`` is ``true`` and ``keydown``/``keyup`` handlers fire.

    Parameters
    ----------
    selector:
        A CSS selector string identifying the input element.
    text:
        The string to type, one character at a time.

    Returns
    -------
    dict
        ``{"typed": int}`` — number of characters dispatched.

    Raises
    ------
    ValueError
        If no element matches *selector*.
    """
    engine = _get_engine()
    session = engine.session
    x, y = _resolve_element_center(session, selector)
    # Click to focus the element before typing.
    session.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": "none",
    })
    session.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
    })
    session.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
    })
    for char in text:
        session.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "text": char, "unmodifiedText": char,
        })
        session.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "text": char, "unmodifiedText": char,
        })
    return {"typed": len(text)}


@mcp.tool()
def fill(selector: str, value: str) -> dict:
    """Set the value of *selector* and dispatch input/change events.

    Unlike ``type()``, this sets the element's ``.value`` property directly and
    fires synthetic ``input`` and ``change`` events — suitable for programmatic
    form-fill where character-by-character key replay is undesirable (e.g. large
    text, password fields, date pickers).

    Uses the native input value setter so that React-controlled inputs register
    the change correctly before the events are dispatched.

    Parameters
    ----------
    selector:
        A CSS selector string identifying the input/textarea/select element.
    value:
        The value to set.

    Returns
    -------
    dict
        ``{"filled": True}`` on success.

    Raises
    ------
    ValueError
        If no element matches *selector*.
    RuntimeError
        If the JS evaluation raises an exception.
    """
    engine = _get_engine()
    session = engine.session
    # Scroll into view; coordinates are not needed for value-setting.
    _resolve_element_center(session, selector)
    expr = (
        f"(function(){{"
        f"  var el = document.querySelector({selector!r});"
        f"  if (!el) return false;"
        f"  var nativeInputValueSetter = Object.getOwnPropertyDescriptor("
        f"    Object.getPrototypeOf(el), 'value')?.set;"
        f"  if (nativeInputValueSetter) {{"
        f"    nativeInputValueSetter.call(el, {value!r});"
        f"  }} else {{"
        f"    el.value = {value!r};"
        f"  }}"
        f"  el.dispatchEvent(new Event('input', {{bubbles: true}}));"
        f"  el.dispatchEvent(new Event('change', {{bubbles: true}}));"
        f"  return true;"
        f"}})();"
    )
    result = session.send(
        "Runtime.evaluate",
        {"expression": expr, "awaitPromise": False, "returnByValue": True},
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(
            f"JS exception in fill() for selector {selector!r}: "
            f"{result['exceptionDetails']}"
        )
    return {"filled": True}


@mcp.tool()
def press_key(key: str) -> dict:
    """Press a keyboard key on the currently focused element.

    Dispatches ``Input.dispatchKeyEvent`` keyDown + keyUp for *key*.  Use
    standard DOM ``KeyboardEvent.key`` values: ``"Enter"``, ``"Tab"``,
    ``"Escape"``, ``"ArrowDown"``, ``" "`` (Space), etc.

    Parameters
    ----------
    key:
        A DOM ``KeyboardEvent.key`` value.

    Returns
    -------
    dict
        ``{"key": key}`` — the key that was pressed.
    """
    engine = _get_engine()
    session = engine.session
    session.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
    session.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
    return {"key": key}


@mcp.tool()
def select_option(selector: str, value: str) -> dict:
    """Select an option in a ``<select>`` element by value.

    Sets the element's ``.value`` to *value* and dispatches a ``change`` event
    so that framework listeners are notified.

    Parameters
    ----------
    selector:
        A CSS selector identifying the ``<select>`` element.
    value:
        The ``value`` attribute of the ``<option>`` to select.

    Returns
    -------
    dict
        ``{"selected": value}`` on success.

    Raises
    ------
    ValueError
        If no element matches *selector* or the value is not among the options.
    RuntimeError
        If the JS evaluation raises an exception.
    """
    engine = _get_engine()
    session = engine.session
    _resolve_element_center(session, selector)
    expr = (
        f"(function(){{"
        f"  var el = document.querySelector({selector!r});"
        f"  if (!el) return {{ok: false, reason: 'not_found'}};"
        f"  var opts = Array.from(el.options).map(function(o){{return o.value;}});"
        f"  if (!opts.includes({value!r})) return {{ok: false, reason: 'invalid_value', options: opts}};"
        f"  el.value = {value!r};"
        f"  el.dispatchEvent(new Event('change', {{bubbles: true}}));"
        f"  return {{ok: true}};"
        f"}})();"
    )
    result = session.send(
        "Runtime.evaluate",
        {"expression": expr, "awaitPromise": False, "returnByValue": True},
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(
            f"JS exception in select_option() for selector {selector!r}: "
            f"{result['exceptionDetails']}"
        )
    rv = result.get("result", {}).get("value", {})
    if not rv.get("ok"):
        reason = rv.get("reason", "unknown")
        if reason == "not_found":
            raise ValueError(f"No element found for selector {selector!r}")
        raise ValueError(
            f"Value {value!r} not in options for {selector!r}: {rv.get('options')}"
        )
    return {"selected": value}


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mcp.run()
