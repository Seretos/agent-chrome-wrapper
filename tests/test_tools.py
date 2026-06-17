"""Unit tests for the MVP tool surface in chrome_wrapper_plugin.server.

All CDP I/O is mocked — no real Chrome is required.  The fake engine
pattern mirrors tests/test_server.py: patch _get_engine on the module,
attach a MagicMock as engine.session.
"""
from __future__ import annotations

import base64
import collections
import threading
from pathlib import Path
from unittest import mock

import pytest

import chrome_wrapper_plugin.server as server_module
from chrome_wrapper_plugin.server import (
    ChromeEngine,
    _attach_buffers,
    cdp,
    evaluate_js,
    get_console_logs,
    get_network_log,
    get_page_info,
    navigate,
    screenshot,
)
from mcp.server.fastmcp import Image


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_engine_with_session() -> ChromeEngine:
    """Return a ChromeEngine with a fresh MagicMock attached as .session."""
    engine = ChromeEngine(
        proc=None,
        port=9222,
        user_data_dir=Path("/tmp/udd"),
        session_id="test-session",
    )
    engine.session = mock.MagicMock()
    return engine


# ── navigate ──────────────────────────────────────────────────────────────────

class TestNavigate:
    """Tests for the navigate() tool."""

    def test_navigate_sends_page_navigate(self):
        """navigate() must call session.send with Page.navigate and the given url."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"frameId": "F1", "loaderId": "L1"}

        # Make add_listener immediately invoke the callback so the threading.Event
        # is set and wait() returns without blocking.
        def _instant_listener(event_name, cb):
            if event_name == "Page.loadEventFired":
                cb({})

        engine.session.add_listener.side_effect = _instant_listener

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = navigate("https://example.com")

        engine.session.send.assert_called_once_with(
            "Page.navigate", {"url": "https://example.com"}
        )
        assert result == {"frameId": "F1", "loaderId": "L1"}

    def test_navigate_registers_and_removes_listener(self):
        """navigate() must register and always remove the Page.loadEventFired listener."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {}

        captured_cb = {}

        def _capture_listener(event_name, cb):
            if event_name == "Page.loadEventFired":
                captured_cb["fn"] = cb
                cb({})  # fire immediately so we don't block

        engine.session.add_listener.side_effect = _capture_listener

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            navigate("https://example.com")

        engine.session.add_listener.assert_called_once_with(
            "Page.loadEventFired", captured_cb["fn"]
        )
        engine.session.remove_listener.assert_called_once_with(
            "Page.loadEventFired", captured_cb["fn"]
        )

    def test_navigate_removes_listener_on_exception(self):
        """navigate() must remove the listener even when session.send raises."""
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = RuntimeError("CDP boom")

        captured_cb = {}

        def _capture_listener(event_name, cb):
            if event_name == "Page.loadEventFired":
                captured_cb["fn"] = cb

        engine.session.add_listener.side_effect = _capture_listener

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            with pytest.raises(RuntimeError, match="CDP boom"):
                navigate("https://example.com")

        engine.session.remove_listener.assert_called_once_with(
            "Page.loadEventFired", captured_cb["fn"]
        )

    def test_navigate_invalid_wait_until_raises_value_error(self):
        """navigate() must raise ValueError for wait_until values other than 'load'."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            with pytest.raises(ValueError, match="wait_until="):
                navigate("https://example.com", wait_until="networkidle")

        # No CDP call should have been made
        engine.session.send.assert_not_called()

    def test_navigate_wait_until_load_is_accepted(self):
        """navigate() must NOT raise for the default wait_until='load'."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {}

        def _instant_listener(event_name, cb):
            if event_name == "Page.loadEventFired":
                cb({})

        engine.session.add_listener.side_effect = _instant_listener

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            navigate("https://example.com", wait_until="load")  # must not raise

    def test_navigate_raises_timeout_error_when_load_event_not_fired(self):
        """navigate() must raise TimeoutError when Page.loadEventFired never fires.

        Regression test: previously wait()'s False return value was discarded,
        causing a silent success on page-load timeout.
        The threading.Event.wait is patched to return False immediately so the
        test does not block for 30 seconds.
        """
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"frameId": "F1", "loaderId": "L1"}

        # add_listener stores the callback but never fires it — simulates a page
        # that never reaches loadEventFired within the timeout window.
        captured_cb: dict = {}

        def _capture_only(event_name, cb):
            if event_name == "Page.loadEventFired":
                captured_cb["fn"] = cb

        engine.session.add_listener.side_effect = _capture_only

        # Patch threading.Event.wait to return False immediately (timeout expired)
        with mock.patch.object(threading.Event, "wait", return_value=False):
            with mock.patch.object(server_module, "_get_engine", return_value=engine):
                with pytest.raises(TimeoutError, match="Page.loadEventFired not received within 30s"):
                    navigate("https://slow.example.com")

        # Even on TimeoutError the listener must have been removed (finally block).
        engine.session.remove_listener.assert_called_once_with(
            "Page.loadEventFired", captured_cb["fn"]
        )


# ── get_page_info ─────────────────────────────────────────────────────────────

class TestGetPageInfo:
    """Tests for the get_page_info() tool."""

    def test_returns_url_and_title(self):
        """get_page_info() must return a dict with at least url and title."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "targetInfo": {
                "url": "https://example.com",
                "title": "Example Domain",
                "targetId": "T1",
                "type": "page",
            }
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_page_info()

        engine.session.send.assert_called_once_with("Target.getTargetInfo", {})
        assert result["url"] == "https://example.com"
        assert result["title"] == "Example Domain"

    def test_url_and_title_are_in_result_keys(self):
        """get_page_info() result must contain both 'url' and 'title' keys."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "targetInfo": {"url": "about:blank", "title": ""}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_page_info()

        assert "url" in result
        assert "title" in result


# ── screenshot ────────────────────────────────────────────────────────────────

class TestScreenshot:
    """Tests for the screenshot() tool."""

    def _make_png_b64(self) -> str:
        """Return a base64-encoded string of a few arbitrary bytes (fake PNG)."""
        return base64.b64encode(b"\x89PNG fake").decode()

    def test_returns_image_object(self):
        """screenshot() must return a FastMCP Image instance."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"data": self._make_png_b64()}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        assert isinstance(result, Image)

    def test_image_has_png_format(self):
        """screenshot() Image must be created with format='png' (stored as _format)."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"data": self._make_png_b64()}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        # FastMCP Image stores format in _format; _mime_type is derived from it.
        assert result._format == "png"
        assert result._mime_type == "image/png"

    def test_image_data_is_decoded_bytes(self):
        """screenshot() must base64-decode the CDP data and embed it as bytes."""
        raw_bytes = b"\x89PNG\r\n\x1a\n fake content"
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "data": base64.b64encode(raw_bytes).decode()
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        assert result.data == raw_bytes

    def test_calls_page_capture_screenshot(self):
        """screenshot() must send Page.captureScreenshot with format='png'."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"data": self._make_png_b64()}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            screenshot()

        engine.session.send.assert_called_once_with(
            "Page.captureScreenshot", {"format": "png"}
        )

    def test_full_page_flag_accepted_without_error(self):
        """screenshot(full_page=True) must not raise — it's silently ignored for MVP."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"data": self._make_png_b64()}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            screenshot(full_page=True)  # must not raise


# ── evaluate_js ───────────────────────────────────────────────────────────────

class TestEvaluateJs:
    """Tests for the evaluate_js() tool."""

    def test_sends_runtime_evaluate(self):
        """evaluate_js() must call Runtime.evaluate with correct params."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"type": "string", "value": "hello"}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = evaluate_js("'hello'")

        engine.session.send.assert_called_once_with(
            "Runtime.evaluate",
            {"expression": "'hello'", "awaitPromise": True, "returnByValue": True},
        )

    def test_returns_cdp_result_passthrough(self):
        """evaluate_js() must return the raw CDP result dict unchanged."""
        cdp_result = {"result": {"type": "number", "value": 42}}
        engine = _fake_engine_with_session()
        engine.session.send.return_value = cdp_result

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = evaluate_js("21 + 21")

        assert result is cdp_result

    def test_await_promise_is_true(self):
        """evaluate_js() must set awaitPromise=True so Promise results are resolved."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"type": "undefined"}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            evaluate_js("Promise.resolve(1)")

        # call_args is (args, kwargs); params is the second positional arg
        call_params = engine.session.send.call_args[0][1]
        assert call_params["awaitPromise"] is True


# ── cdp (raw passthrough) ─────────────────────────────────────────────────────

class TestCdpRaw:
    """Tests for the cdp() raw-passthrough tool."""

    def test_passes_method_and_params_to_session(self):
        """cdp() must forward method and params unchanged to session.send."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"root": {"nodeId": 1}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = cdp("DOM.getDocument", {"depth": 1})

        engine.session.send.assert_called_once_with("DOM.getDocument", {"depth": 1})

    def test_returns_session_result_unchanged(self):
        """cdp() must return exactly what session.send returns."""
        expected = {"some": "result", "nested": {"x": 42}}
        engine = _fake_engine_with_session()
        engine.session.send.return_value = expected

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = cdp("Foo.bar", {})

        assert result is expected

    def test_empty_params_dict(self):
        """cdp() must work with an empty params dict."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = cdp("Page.reload", {})

        assert result == {}


# ── get_console_logs ──────────────────────────────────────────────────────────

def _fake_engine_with_real_buffers() -> ChromeEngine:
    """Return a ChromeEngine with real deque buffers and a MagicMock session.

    Uses real collections.deque instances (not mocked) so drain semantics are
    observable in the tests below.
    """
    import collections
    engine = ChromeEngine(
        proc=None,
        port=9222,
        user_data_dir=Path("/tmp/udd"),
        session_id="test-session",
    )
    engine.session = mock.MagicMock()
    # Replace the default deques with fresh ones (same maxlen=500) to be explicit
    engine.console_buffer = collections.deque(maxlen=500)
    engine.network_buffer = collections.deque(maxlen=500)
    return engine


class TestGetConsoleLogs:
    """Tests for the get_console_logs() tool — drain semantics included."""

    def test_returns_empty_list_when_buffer_is_empty(self):
        """get_console_logs() returns [] when no console events have been captured."""
        engine = _fake_engine_with_real_buffers()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_console_logs()

        assert result == []

    def test_returns_buffered_entries(self):
        """get_console_logs() returns all entries currently in the console buffer."""
        engine = _fake_engine_with_real_buffers()
        entry1 = {"type": "consoleAPI", "level": "log", "args": [], "timestamp": 1.0}
        entry2 = {"type": "exception", "text": "Uncaught", "exception": None,
                   "url": "https://example.com", "lineNumber": 10, "timestamp": 2.0}
        engine.console_buffer.append(entry1)
        engine.console_buffer.append(entry2)

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_console_logs()

        assert result == [entry1, entry2]

    def test_drains_buffer_on_read(self):
        """A second call to get_console_logs() returns [] — the buffer is drained on first read."""
        engine = _fake_engine_with_real_buffers()
        engine.console_buffer.append({"type": "consoleAPI", "level": "warn",
                                      "args": [], "timestamp": 3.0})

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            first = get_console_logs()
            second = get_console_logs()

        assert len(first) == 1
        assert second == []


# ── get_network_log ───────────────────────────────────────────────────────────

class TestGetNetworkLog:
    """Tests for the get_network_log() tool — drain semantics included."""

    def test_returns_empty_list_when_buffer_is_empty(self):
        """get_network_log() returns [] when no network events have been captured."""
        engine = _fake_engine_with_real_buffers()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_network_log()

        assert result == []

    def test_returns_buffered_entries(self):
        """get_network_log() returns all entries currently in the network buffer."""
        engine = _fake_engine_with_real_buffers()
        entry1 = {"event": "responseReceived", "requestId": "r1",
                   "url": "https://example.com/api", "status": 200,
                   "mimeType": "application/json", "timing": None, "timestamp": 1.5}
        entry2 = {"event": "loadingFailed", "requestId": "r2",
                   "url": "https://example.com/missing", "errorText": "net::ERR_NAME_NOT_RESOLVED",
                   "canceled": False, "timestamp": 2.5}
        engine.network_buffer.append(entry1)
        engine.network_buffer.append(entry2)

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_network_log()

        assert result == [entry1, entry2]

    def test_drains_buffer_on_read(self):
        """A second call to get_network_log() returns [] — the buffer is drained on first read."""
        engine = _fake_engine_with_real_buffers()
        engine.network_buffer.append({"event": "responseReceived", "requestId": "r3",
                                       "url": "https://example.com", "status": 404,
                                       "mimeType": "text/html", "timing": None,
                                       "timestamp": 4.0})

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            first = get_network_log()
            second = get_network_log()

        assert len(first) == 1
        assert second == []


# ── _attach_buffers callback-level tests ─────────────────────────────────────
#
# These tests call _attach_buffers() on a real ChromeEngine with a MagicMock
# session, capture the registered callbacks via add_listener.call_args_list,
# then invoke each callback with a representative raw CDP params dict (the
# real shape Chrome sends) and assert the normalised output in the buffer.

def _make_engine_for_attach() -> ChromeEngine:
    """Return a fresh ChromeEngine with a MagicMock session for callback tests."""
    engine = ChromeEngine(
        proc=None,
        port=9222,
        user_data_dir=Path("/tmp/udd"),
        session_id="cb-test",
    )
    engine.session = mock.MagicMock()
    return engine


def _extract_callbacks(engine: ChromeEngine) -> dict:
    """Call _attach_buffers and return a {event_name: callback} mapping.

    Inspects engine.session.add_listener.call_args_list after the call.
    """
    _attach_buffers(engine)
    callbacks = {}
    for call in engine.session.add_listener.call_args_list:
        event_name, cb = call[0]
        callbacks[event_name] = cb
    return callbacks


class TestAttachBuffersCallbacks:
    """Verify that each CDP callback registered by _attach_buffers normalises
    raw params correctly and appends the right entry to the buffer."""

    def test_console_api_called_normalises_entry(self):
        """Runtime.consoleAPICalled callback stores CDP 'type' as 'level'."""
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        raw_params = {
            "type": "warning",
            "args": [{"type": "string", "value": "bad input"}],
            "timestamp": 1001.5,
            "executionContextId": 1,
        }
        cbs["Runtime.consoleAPICalled"](raw_params)

        assert len(engine.console_buffer) == 1
        entry = engine.console_buffer[0]
        assert entry["type"] == "consoleAPI"
        # CDP 'type' field is stored as 'level' in the normalised entry.
        assert entry["level"] == "warning"
        assert entry["args"] == raw_params["args"]
        assert entry["timestamp"] == 1001.5

    def test_exception_thrown_normalises_entry(self):
        """Runtime.exceptionThrown callback extracts fields from exceptionDetails."""
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        raw_params = {
            "timestamp": 2002.0,
            "exceptionDetails": {
                "text": "Uncaught ReferenceError: x is not defined",
                "exception": {"type": "object", "subtype": "error"},
                "url": "https://example.com/app.js",
                "lineNumber": 42,
                "columnNumber": 7,
            },
        }
        cbs["Runtime.exceptionThrown"](raw_params)

        assert len(engine.console_buffer) == 1
        entry = engine.console_buffer[0]
        assert entry["type"] == "exception"
        assert entry["text"] == "Uncaught ReferenceError: x is not defined"
        assert entry["url"] == "https://example.com/app.js"
        assert entry["lineNumber"] == 42
        assert entry["timestamp"] == 2002.0

    def test_log_entry_added_normalises_entry(self):
        """Log.entryAdded callback extracts fields from nested 'entry' dict."""
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        raw_params = {
            "entry": {
                "level": "error",
                "text": "Mixed Content: blocked",
                "source": "security",
                "url": "http://insecure.example.com/img.png",
                "timestamp": 3003.0,
            }
        }
        cbs["Log.entryAdded"](raw_params)

        assert len(engine.console_buffer) == 1
        entry = engine.console_buffer[0]
        assert entry["type"] == "log"
        assert entry["level"] == "error"
        assert entry["text"] == "Mixed Content: blocked"
        assert entry["source"] == "security"
        assert entry["url"] == "http://insecure.example.com/img.png"

    def test_request_will_be_sent_populates_side_map(self):
        """Network.requestWillBeSent callback stores requestId→url in request_url_map."""
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        raw_params = {
            "requestId": "req-abc",
            "frameId": "frame-1",
            "type": "Document",
            "request": {
                "url": "https://example.com/page",
                "method": "GET",
                "headers": {},
            },
            "timestamp": 100.0,
        }
        cbs["Network.requestWillBeSent"](raw_params)

        assert engine.request_url_map.get("req-abc") == "https://example.com/page"
        # Should NOT add anything to network_buffer
        assert len(engine.network_buffer) == 0

    def test_response_received_normalises_entry(self):
        """Network.responseReceived callback extracts url/status/mimeType from 'response'."""
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        # Pre-populate side-map (as requestWillBeSent would have)
        engine.request_url_map["req-xyz"] = "https://api.example.com/data"

        raw_params = {
            "requestId": "req-xyz",
            "frameId": "frame-1",
            "timestamp": 200.5,
            "type": "XHR",
            "response": {
                "url": "https://api.example.com/data",
                "status": 200,
                "statusText": "OK",
                "mimeType": "application/json",
                "timing": {"receiveHeadersEnd": 50.0},
            },
        }
        cbs["Network.responseReceived"](raw_params)

        assert len(engine.network_buffer) == 1
        entry = engine.network_buffer[0]
        assert entry["event"] == "responseReceived"
        assert entry["requestId"] == "req-xyz"
        assert entry["url"] == "https://api.example.com/data"
        assert entry["status"] == 200
        assert entry["mimeType"] == "application/json"
        assert entry["timing"] == {"receiveHeadersEnd": 50.0}
        # Side-map entry should be pruned after response received
        assert "req-xyz" not in engine.request_url_map

    def test_loading_failed_url_populated_from_side_map(self):
        """Regression: Network.loadingFailed url comes from requestWillBeSent side-map.

        Before the fix, _on_loading_failed used params.get('documentURL') which
        is not a field on Network.loadingFailed — it always returned None.
        This test asserts the url IS populated via the side-map.
        """
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        # Simulate requestWillBeSent having been received first
        cbs["Network.requestWillBeSent"]({
            "requestId": "req-fail",
            "request": {"url": "https://missing.example.com/resource"},
            "timestamp": 300.0,
        })
        assert engine.request_url_map.get("req-fail") == "https://missing.example.com/resource"

        # Now simulate loadingFailed — note: NO 'url' or 'documentURL' at top level
        raw_params = {
            "requestId": "req-fail",
            "timestamp": 301.0,
            "type": "Document",
            "errorText": "net::ERR_NAME_NOT_RESOLVED",
            "canceled": False,
            "blockedReason": None,
        }
        cbs["Network.loadingFailed"](raw_params)

        assert len(engine.network_buffer) == 1
        entry = engine.network_buffer[0]
        assert entry["event"] == "loadingFailed"
        assert entry["requestId"] == "req-fail"
        # This is the regression assertion: url must be populated from the side-map
        assert entry["url"] == "https://missing.example.com/resource"
        assert entry["errorText"] == "net::ERR_NAME_NOT_RESOLVED"
        assert entry["canceled"] is False
        # Side-map entry should be pruned after failure
        assert "req-fail" not in engine.request_url_map

    def test_loading_failed_url_is_none_when_no_prior_request(self):
        """loadingFailed with no matching requestWillBeSent gives url=None (defensive)."""
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        raw_params = {
            "requestId": "req-unknown",
            "timestamp": 400.0,
            "errorText": "net::ERR_CONNECTION_REFUSED",
            "canceled": False,
        }
        cbs["Network.loadingFailed"](raw_params)

        assert len(engine.network_buffer) == 1
        entry = engine.network_buffer[0]
        assert entry["url"] is None

    def test_request_will_be_sent_missing_request_field_is_defensive(self):
        """requestWillBeSent with no 'request' field does not crash and skips side-map."""
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        # Malformed params with no 'request' key
        cbs["Network.requestWillBeSent"]({"requestId": "req-bad", "timestamp": 500.0})

        assert "req-bad" not in engine.request_url_map

    def test_all_six_listeners_registered(self):
        """_attach_buffers registers exactly 6 CDP event listeners."""
        engine = _make_engine_for_attach()
        _attach_buffers(engine)

        registered_events = [
            call[0][0] for call in engine.session.add_listener.call_args_list
        ]
        expected = {
            "Runtime.consoleAPICalled",
            "Runtime.exceptionThrown",
            "Log.entryAdded",
            "Network.requestWillBeSent",
            "Network.responseReceived",
            "Network.loadingFailed",
        }
        assert set(registered_events) == expected
        assert len(registered_events) == 6

    def test_atomic_swap_console_buffer_survives_concurrent_append(self):
        """Atomic-swap drain: items appended to the NEW buffer after swap are not lost.

        Simulates the race where the WS thread appends to engine.console_buffer
        after the swap — the new item should appear on the NEXT drain, not be dropped.
        """
        engine = _make_engine_for_attach()
        cbs = _extract_callbacks(engine)

        # Put one entry in the buffer
        cbs["Runtime.consoleAPICalled"]({
            "type": "log", "args": [], "timestamp": 600.0
        })

        # Drain: atomic swap replaces the buffer
        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            first = get_console_logs()

        assert len(first) == 1

        # Now the callback appends to the NEW (post-swap) buffer
        cbs["Runtime.consoleAPICalled"]({
            "type": "error", "args": [], "timestamp": 601.0
        })

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            second = get_console_logs()

        assert len(second) == 1
        assert second[0]["level"] == "error"
