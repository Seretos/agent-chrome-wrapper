"""Tests for chrome_wrapper_plugin.cdp — CDPSession transport layer.

All tests are mock-only: no real Chrome, no real WebSocket connection.
Follows the setup_method / teardown_method pattern of test_server.py and
test_chrome_process.py.
"""

from __future__ import annotations

import json
import threading
from unittest import mock

import pytest

from chrome_wrapper_plugin.cdp import CDPError, CDPSession


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_targets_response(ws_url: str = "ws://127.0.0.1:9222/devtools/page/ABC") -> bytes:
    """Return JSON bytes as Chrome's /json endpoint would."""
    targets = [{"type": "page", "webSocketDebuggerUrl": ws_url}]
    return json.dumps(targets).encode()


def _make_cdp_response(msg_id: int, result: dict) -> str:
    return json.dumps({"id": msg_id, "result": result})


def _make_cdp_error(msg_id: int, code: int, message: str) -> str:
    return json.dumps({"id": msg_id, "error": {"code": code, "message": message}})


def _make_cdp_event(method: str, params: dict) -> str:
    return json.dumps({"method": method, "params": params})


# ── TestCDPSessionConnect ─────────────────────────────────────────────────────

class TestCDPSessionConnect:
    """connect() fetches /json, opens a WebSocket, fires on_open, then enables domains."""

    def setup_method(self):
        self._session = None

    def teardown_method(self):
        pass

    def test_connect_sends_enable_commands_in_order(self):
        """Page/Runtime/DOM/Target .enable are sent after WebSocket handshake.

        Deterministic: on_open is fired synchronously inside the mock
        run_forever() so there is no wall-clock sleep and no race.
        send() is patched via mock.patch.object before connect() is called
        so the real send path is not exercised (no real WS needed).
        """
        session = CDPSession(port=9222, timeout=5.0)

        # Fake urllib response for /json
        fake_resp = mock.MagicMock()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = mock.MagicMock(return_value=False)
        fake_resp.read.return_value = _make_targets_response()

        def fake_ws_app_ctor(url, *, on_open, on_message, on_error, on_close):
            app = mock.MagicMock()
            # run_forever fires on_open synchronously before returning so the
            # connected_event is set before connect()'s event.wait() is reached
            # (the thread is joined conceptually — it returns immediately here).
            def _run_forever():
                on_open(app)
            app.run_forever.side_effect = _run_forever
            return app

        sent_methods: list[str] = []

        def fake_send(method, params=None):
            sent_methods.append(method)
            return {}

        with (
            mock.patch("urllib.request.urlopen", return_value=fake_resp),
            mock.patch("websocket.WebSocketApp", side_effect=fake_ws_app_ctor),
            mock.patch.object(session, "send", side_effect=fake_send),
        ):
            session.connect()

        assert sent_methods == [
            "Page.enable",
            "Runtime.enable",
            "DOM.enable",
            "Target.enable",
        ]

    def test_connect_raises_if_no_page_target(self):
        """RuntimeError when /json returns targets but none with type='page'."""
        session = CDPSession(port=9222)

        other_targets = [{"type": "browser", "webSocketDebuggerUrl": "ws://..."}]
        fake_resp = mock.MagicMock()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = mock.MagicMock(return_value=False)
        fake_resp.read.return_value = json.dumps(other_targets).encode()

        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            with pytest.raises(RuntimeError, match="No page target found"):
                session.connect()


# ── TestCDPSessionSend ────────────────────────────────────────────────────────

class TestCDPSessionSend:
    """send() dispatches a JSON message and returns the result field."""

    def setup_method(self):
        self.session = CDPSession(port=9222, timeout=5.0)
        # Bypass connect() — inject a mock WS directly
        self.mock_ws = mock.MagicMock()
        self.session._ws = self.mock_ws
        self.session._connected_event.set()

    def teardown_method(self):
        pass

    def test_send_returns_result_dict(self):
        """send() returns the result dict from the response message."""
        result = {}

        def _call_send():
            nonlocal result
            result["value"] = self.session.send("Runtime.evaluate", {"expression": "1+1"})

        # Run send in a background thread (it blocks on event.wait)
        t = threading.Thread(target=_call_send)
        t.start()

        # Give the thread a moment to register the pending entry
        import time
        time.sleep(0.05)

        # Inject a matching response via _on_message
        self.session._on_message(self.mock_ws, _make_cdp_response(1, {"value": 2}))

        t.join(timeout=2.0)
        assert not t.is_alive(), "send() did not return"
        assert result["value"] == {"value": 2}

    def test_send_serialises_payload_correctly(self):
        """The message sent to _ws.send has the right id/method/params."""
        sent_payloads: list[dict] = []
        self.mock_ws.send.side_effect = lambda p: sent_payloads.append(json.loads(p))

        def _call_send():
            try:
                self.session.send("Page.navigate", {"url": "https://example.com"})
            except TimeoutError:
                pass  # timeout is fine — we only care about the outgoing payload

        t = threading.Thread(target=_call_send)
        t.start()

        import time
        time.sleep(0.05)

        # Resolve with response so thread exits cleanly
        self.session._on_message(self.mock_ws, _make_cdp_response(1, {}))
        t.join(timeout=2.0)

        assert len(sent_payloads) == 1
        p = sent_payloads[0]
        assert p["id"] == 1
        assert p["method"] == "Page.navigate"
        assert p["params"] == {"url": "https://example.com"}


# ── TestCDPSessionError ───────────────────────────────────────────────────────

class TestCDPSessionError:
    """CDPError is raised when Chrome returns an error field."""

    def setup_method(self):
        self.session = CDPSession(port=9222, timeout=5.0)
        self.mock_ws = mock.MagicMock()
        self.session._ws = self.mock_ws
        self.session._connected_event.set()

    def teardown_method(self):
        pass

    def test_cdp_error_raised_with_code_in_message(self):
        """CDPError message contains the error code."""
        exc_holder: list = []

        def _call_send():
            try:
                self.session.send("Target.activateTarget", {"targetId": "bad"})
            except CDPError as e:
                exc_holder.append(e)

        t = threading.Thread(target=_call_send)
        t.start()

        import time
        time.sleep(0.05)

        self.session._on_message(
            self.mock_ws,
            _make_cdp_error(1, -32000, "No such target"),
        )

        t.join(timeout=2.0)
        assert len(exc_holder) == 1
        assert "-32000" in str(exc_holder[0])
        assert "No such target" in str(exc_holder[0])


# ── TestCDPSessionTimeout ─────────────────────────────────────────────────────

class TestCDPSessionTimeout:
    """TimeoutError is raised when no response arrives in time."""

    def setup_method(self):
        # Use a very short timeout so the test runs fast
        self.session = CDPSession(port=9222, timeout=0.05)
        self.mock_ws = mock.MagicMock()
        self.session._ws = self.mock_ws
        self.session._connected_event.set()

    def teardown_method(self):
        pass

    def test_timeout_raises_and_clears_pending(self):
        """TimeoutError is raised and the pending entry is removed."""
        with pytest.raises(TimeoutError, match="timed out"):
            self.session.send("Page.navigate", {"url": "https://slow.example"})

        # _pending should be empty after timeout
        assert self.session._pending == {}

    def test_timeout_message_contains_method_name(self):
        """The TimeoutError message names the CDP method."""
        with pytest.raises(TimeoutError) as exc_info:
            self.session.send("DOM.getDocument")

        assert "DOM.getDocument" in str(exc_info.value)


# ── TestCDPSessionListeners ───────────────────────────────────────────────────

class TestCDPSessionListeners:
    """add_listener / remove_listener and event dispatch."""

    def setup_method(self):
        self.session = CDPSession(port=9222)
        self.mock_ws = mock.MagicMock()
        self.session._ws = self.mock_ws

    def teardown_method(self):
        pass

    def test_listener_receives_event_params(self):
        """Listener is called with the event's params dict."""
        received: list[dict] = []
        cb = received.append

        self.session.add_listener("Page.loadEventFired", cb)
        self.session._on_message(
            self.mock_ws,
            _make_cdp_event("Page.loadEventFired", {"timestamp": 1.23}),
        )

        assert received == [{"timestamp": 1.23}]

    def test_remove_listener_stops_dispatch(self):
        """After remove_listener, cb is not called on subsequent events."""
        call_count = [0]

        def cb(params):
            call_count[0] += 1

        self.session.add_listener("Page.loadEventFired", cb)
        self.session._on_message(
            self.mock_ws,
            _make_cdp_event("Page.loadEventFired", {}),
        )
        assert call_count[0] == 1

        self.session.remove_listener("Page.loadEventFired", cb)
        self.session._on_message(
            self.mock_ws,
            _make_cdp_event("Page.loadEventFired", {}),
        )
        # Still exactly 1 — second event not dispatched
        assert call_count[0] == 1

    def test_remove_listener_noop_if_absent(self):
        """remove_listener does not raise when the callback was never added."""
        cb = lambda params: None  # noqa: E731
        self.session.remove_listener("Page.loadEventFired", cb)  # must not raise

    def test_listener_for_unregistered_event_is_silently_ignored(self):
        """Events with no registered listener are dropped without error."""
        self.session._on_message(
            self.mock_ws,
            _make_cdp_event("Network.requestWillBeSent", {"requestId": "x"}),
        )  # no assertion needed — must not raise


# ── TestCDPSessionClose ───────────────────────────────────────────────────────

class TestCDPSessionClose:
    """close() calls _ws.close() exactly once."""

    def setup_method(self):
        self.session = CDPSession(port=9222)
        self.mock_ws = mock.MagicMock()
        self.session._ws = self.mock_ws

    def teardown_method(self):
        pass

    def test_close_calls_ws_close(self):
        self.session.close()
        self.mock_ws.close.assert_called_once()

    def test_close_clears_pending(self):
        """_pending is emptied so no stale entries survive after close."""
        # Inject a fake pending entry
        import threading
        fake_event = threading.Event()
        self.session._pending[42] = (fake_event, [None])

        self.session.close()

        assert self.session._pending == {}


# ── TestCDPSessionConnectError ────────────────────────────────────────────────

class TestCDPSessionConnectError:
    """connect() fails fast and cleans up when the WebSocket reports an error."""

    def setup_method(self):
        self._session = None

    def teardown_method(self):
        pass

    def _make_fake_resp(self, ws_url: str = "ws://127.0.0.1:9222/devtools/page/ABC"):
        """Return a mock urllib response yielding a single page target."""
        fake_resp = mock.MagicMock()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = mock.MagicMock(return_value=False)
        fake_resp.read.return_value = _make_targets_response(ws_url)
        return fake_resp

    def test_connect_reraises_ws_error_verbatim(self):
        """connect() re-raises the exact same exception object injected via on_error."""
        session = CDPSession(port=9222, timeout=5.0)
        injected_error = ConnectionError("403 Forbidden")

        def fake_ws_app_ctor(url, *, on_open, on_message, on_error, on_close):
            app = mock.MagicMock()

            def _run_forever():
                # Simulate Chrome rejecting the WS upgrade with a 403 error
                on_error(app, injected_error)

            app.run_forever.side_effect = _run_forever
            return app

        with (
            mock.patch("urllib.request.urlopen", return_value=self._make_fake_resp()),
            mock.patch("websocket.WebSocketApp", side_effect=fake_ws_app_ctor),
        ):
            with pytest.raises(ConnectionError) as exc_info:
                session.connect()

        assert exc_info.value is injected_error

    def test_connect_calls_close_on_ws_error(self):
        """connect() calls close() when the WebSocket errors so no orphan WS thread survives."""
        session = CDPSession(port=9222, timeout=5.0)

        def fake_ws_app_ctor(url, *, on_open, on_message, on_error, on_close):
            app = mock.MagicMock()

            def _run_forever():
                on_error(app, ConnectionError("403 Forbidden"))

            app.run_forever.side_effect = _run_forever
            return app

        with (
            mock.patch("urllib.request.urlopen", return_value=self._make_fake_resp()),
            mock.patch("websocket.WebSocketApp", side_effect=fake_ws_app_ctor),
            mock.patch.object(session, "close") as mock_close,
        ):
            with pytest.raises(ConnectionError):
                session.connect()

        mock_close.assert_called_once()

    def test_connect_raises_timeout_when_no_callback_fires(self):
        """connect() raises TimeoutError when WS hangs and neither on_open nor on_error fires."""
        session = CDPSession(port=9222, timeout=0.05)

        def fake_ws_app_ctor(url, *, on_open, on_message, on_error, on_close):
            app = mock.MagicMock()
            # run_forever returns immediately without calling any callback,
            # simulating a WS that hangs without completing or erroring.
            app.run_forever.side_effect = lambda: None
            return app

        with (
            mock.patch("urllib.request.urlopen", return_value=self._make_fake_resp()),
            mock.patch("websocket.WebSocketApp", side_effect=fake_ws_app_ctor),
        ):
            with pytest.raises(TimeoutError, match="did not complete"):
                session.connect()

    def test_connect_on_close_before_open_raises(self):
        """connect() raises ConnectionError immediately when WS closes before handshake completes.

        _on_close records a ConnectionError sentinel in _connect_error and sets
        _connected_event so connect() unblocks and re-raises immediately (fail-fast),
        without waiting for any send() timeout.
        """
        session = CDPSession(port=9222, timeout=5.0)

        def fake_ws_app_ctor(url, *, on_open, on_message, on_error, on_close):
            app = mock.MagicMock()

            def _run_forever():
                # Simulate an abnormal close during handshake, before on_open fires
                on_close(app, 1006, "abnormal")

            app.run_forever.side_effect = _run_forever
            return app

        with (
            mock.patch("urllib.request.urlopen", return_value=self._make_fake_resp()),
            mock.patch("websocket.WebSocketApp", side_effect=fake_ws_app_ctor),
        ):
            with pytest.raises(ConnectionError, match="closed before handshake"):
                session.connect()
