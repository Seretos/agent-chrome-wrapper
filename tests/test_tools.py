"""Unit tests for the MVP tool surface in chrome_wrapper_plugin.server.

All CDP I/O is mocked — no real Chrome is required.  The fake engine
pattern mirrors tests/test_server.py: patch _get_engine on the module,
attach a MagicMock as engine.session.
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path
from unittest import mock

import pytest

import chrome_wrapper_plugin.server as server_module
from chrome_wrapper_plugin.server import (
    ChromeEngine,
    cdp,
    evaluate_js,
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
