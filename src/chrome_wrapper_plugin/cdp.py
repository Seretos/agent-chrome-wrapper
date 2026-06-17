"""CDP transport layer — thin WebSocket client over websocket-client.

Public API (stable contract for ticket #3):
- CDPError       — raised when Chrome returns a CDP-level error response.
- CDPSession     — connect, send, listen, close.

Design notes
------------
- One CDPSession per ChromeEngine (attached/detached in server._get_engine).
- All I/O happens on a daemon thread running WebSocketApp.run_forever();
  the calling thread blocks in send() via threading.Event.wait().
- _id_lock serialises both id generation and _pending dict access (touched
  by both the calling thread and the WS receiver thread).
- _listener_lock serialises _listeners; the listener list is shallow-copied
  before dispatch to avoid lock inversion with user callbacks.
- Timeout is a hard 30 s cap; None is not accepted (fail fast rather than hang).
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Optional

import websocket  # websocket-client package

logger = logging.getLogger(__name__)


class CDPError(Exception):
    """Raised when Chrome returns a CDP error response."""


class CDPSession:
    """A single CDP session connected to one Chrome page target.

    Parameters
    ----------
    port:
        The Chrome remote-debugging port (``--remote-debugging-port``).
    timeout:
        Seconds to wait for a CDP reply before raising ``TimeoutError``.
        Must be a positive float; there is no None / infinite option.
    """

    def __init__(self, port: int, timeout: float = 30.0) -> None:
        if timeout is None:
            raise ValueError("timeout must be a positive float, not None")
        self._port = port
        self._timeout = float(timeout)

        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._connected_event = threading.Event()
        self._connect_error: Optional[Exception] = None

        # Pending CDP commands: msg_id → (event, result_holder)
        # result_holder is a one-element list so we can mutate it from the
        # receiver thread.
        self._id_lock = threading.Lock()
        self._next_id: int = 0
        self._pending: dict[int, tuple[threading.Event, list]] = {}

        # Event listeners: event_name → list of callables
        self._listener_lock = threading.Lock()
        self._listeners: dict[str, list[Callable[[dict], None]]] = {}

    # ── public API ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the WebSocket connection and enable standard CDP domains.

        Fetches ``/json`` from the Chrome endpoint, picks the first page
        target, and connects over WebSocket.  Blocks until the handshake
        completes, then sends the four standard ``*.enable`` commands.

        Raises
        ------
        RuntimeError
            If no page target is found on the debugging port.
        """
        try:
            # Discover the first page target
            url = f"http://127.0.0.1:{self._port}/json"
            with urllib.request.urlopen(url, timeout=10) as resp:
                targets = json.loads(resp.read())

            ws_url: Optional[str] = None
            for t in targets:
                if t.get("type") == "page":
                    ws_url = t.get("webSocketDebuggerUrl")
                    break

            if ws_url is None:
                raise RuntimeError(
                    f"No page target found on Chrome debugging port {self._port}. "
                    f"Targets: {targets!r}"
                )

            self._connected_event.clear()
            self._connect_error = None
            self._ws = websocket.WebSocketApp(
                ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )

            self._ws_thread = threading.Thread(
                target=self._ws.run_forever,
                daemon=True,
                name=f"cdp-ws-{self._port}",
            )
            self._ws_thread.start()

            # Block until on_open fires (or WS error/close, or hard timeout)
            if not self._connected_event.wait(timeout=self._timeout):
                raise TimeoutError(
                    f"WebSocket handshake on port {self._port} did not complete "
                    f"within {self._timeout}s."
                )
            if self._connect_error is not None:
                raise self._connect_error

            # Enable standard CDP domains so we can receive their events
            for domain_enable in (
                "Page.enable",
                "Runtime.enable",
                "DOM.enable",
            ):
                self.send(domain_enable)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the WebSocket connection.  The daemon thread exits automatically."""
        if self._ws is not None:
            self._ws.close()
        with self._id_lock:
            self._pending.clear()

    def send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and return the result dict.

        Parameters
        ----------
        method:
            CDP method, e.g. ``"Page.navigate"``.
        params:
            Optional parameters dict.

        Returns
        -------
        dict
            The ``"result"`` field of the CDP response (may be empty).

        Raises
        ------
        TimeoutError
            If no response arrives within *timeout* seconds.
        CDPError
            If Chrome returns an ``"error"`` field.
        """
        with self._id_lock:
            self._next_id += 1
            msg_id = self._next_id
            event = threading.Event()
            result_holder: list = [None]
            self._pending[msg_id] = (event, result_holder)

        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        assert self._ws is not None, "CDPSession.send() called before connect()"
        self._ws.send(payload)

        if not event.wait(timeout=self._timeout):
            with self._id_lock:
                self._pending.pop(msg_id, None)
            raise TimeoutError(
                f"CDP {method!r} timed out after {self._timeout}s"
            )

        msg = result_holder[0]
        if "error" in msg:
            err = msg["error"]
            raise CDPError(f"{err['code']}: {err['message']}")

        return msg.get("result", {})

    def add_listener(self, event: str, cb: Callable[[dict], None]) -> None:
        """Register *cb* to be called whenever Chrome emits *event*."""
        with self._listener_lock:
            self._listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event: str, cb: Callable[[dict], None]) -> None:
        """Unregister the first matching *cb* for *event*; no-op if absent."""
        with self._listener_lock:
            listeners = self._listeners.get(event, [])
            try:
                listeners.remove(cb)
            except ValueError:
                pass

    # ── WebSocketApp callbacks (run on the WS daemon thread) ─────────────────

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self._connected_event.set()

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            msg: dict = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("CDPSession: received non-JSON message: %r", raw)
            return

        msg_id = msg.get("id")
        if msg_id is not None:
            # Response to a pending send() — pop atomically under the lock,
            # then write result and set the event outside it (no double-acquire,
            # no lock-free window between the read and the pop).
            with self._id_lock:
                entry = self._pending.pop(msg_id, None)
            if entry is not None:
                event, result_holder = entry
                result_holder[0] = msg
                event.set()
            else:
                logger.debug("CDPSession: received response for unknown id %s", msg_id)
            return

        method = msg.get("method")
        if method is not None:
            # CDP event — dispatch to listeners
            with self._listener_lock:
                listeners = list(self._listeners.get(method, []))
            params = msg.get("params", {})
            for cb in listeners:
                try:
                    cb(params)
                except Exception:
                    logger.exception(
                        "CDPSession: listener for %r raised an exception", method
                    )

    def _on_error(self, ws: websocket.WebSocketApp, err: Exception) -> None:
        logger.error("CDPSession: WebSocket error: %s", err)
        self._connect_error = err
        self._connected_event.set()

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        logger.debug(
            "CDPSession: WebSocket closed (status=%s, msg=%r)",
            close_status_code,
            close_msg,
        )
        if not self._connected_event.is_set():
            self._connect_error = ConnectionError(
                f"WebSocket closed before handshake completed "
                f"(status={close_status_code}, msg={close_msg!r})"
            )
            self._connected_event.set()
