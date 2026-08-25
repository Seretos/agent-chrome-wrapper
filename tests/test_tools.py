"""Unit tests for the MVP tool surface in chrome_wrapper_plugin.server.

All CDP I/O is mocked — no real Chrome is required.  The fake engine
pattern mirrors tests/test_server.py: patch _get_engine on the module,
attach a MagicMock as engine.session.
"""
from __future__ import annotations

import base64
import collections
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import chrome_wrapper_plugin.server as server_module
from chrome_wrapper_plugin.server import (
    ChromeEngine,
    _attach_buffers,
    _resolve_element_center,
    cdp,
    click,
    evaluate_js,
    fill,
    get_console_logs,
    get_network_log,
    get_page_info,
    hover,
    navigate,
    press_key,
    screenshot,
    select_option,
    wait_for_selector,
    wait_for_navigation,
    wait_for_network_idle,
)
from chrome_wrapper_plugin.server import type as type_text
from chrome_wrapper_plugin.server import sleep as sleep_tool
from fastmcp.utilities.types import Image


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
    """Tests for the screenshot() tool.

    #41.3: screenshot() becomes a breaking change from a bare Image return to
    a [Image, metadata] pair, so every test that drives the CDP mock now
    supplies a two-element `side_effect` list — one reply for
    Page.captureScreenshot, one for the new Target.getTargetInfo call — in
    place of a single `return_value`.
    """

    def _make_png_b64(self) -> str:
        """Return a base64-encoded string of a few arbitrary bytes (fake PNG,
        no well-formed IHDR — fine for tests that don't assert on dimensions)."""
        return base64.b64encode(b"\x89PNG fake").decode()

    def _make_well_formed_png_bytes(self, width: int, height: int) -> bytes:
        """Build a byte string with a real PNG signature + IHDR chunk carrying
        *width*/*height*, so _png_dimensions() can parse it."""
        return (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")  # IHDR chunk length
            + b"IHDR"
            + width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00restofchunk"
        )

    def _target_info_reply(self, url="http://example.test", title="Example"):
        return {"targetInfo": {"url": url, "title": title}}

    def test_returns_image_object(self):
        """screenshot() must return a FastMCP Image instance as element [0]."""
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = [
            {"data": self._make_png_b64()},
            self._target_info_reply(),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        assert isinstance(result[0], Image)

    def test_image_has_png_format(self):
        """screenshot() Image must be created with format='png', which the
        public fastmcp.Image.to_image_content() surfaces as mimeType='image/png'."""
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = [
            {"data": self._make_png_b64()},
            self._target_info_reply(),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        assert result[0].to_image_content().mimeType == "image/png"

    def test_image_data_is_decoded_bytes(self):
        """screenshot() must base64-decode the CDP data and embed it as bytes."""
        raw_bytes = b"\x89PNG\r\n\x1a\n fake content"
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = [
            {"data": base64.b64encode(raw_bytes).decode()},
            self._target_info_reply(),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        assert result[0].data == raw_bytes

    def test_calls_page_capture_screenshot(self):
        """screenshot() must send Page.captureScreenshot with format='png' as
        its first CDP call."""
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = [
            {"data": self._make_png_b64()},
            self._target_info_reply(),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            screenshot()

        first_call = engine.session.send.call_args_list[0]
        assert first_call[0] == ("Page.captureScreenshot", {"format": "png"})

    def test_full_page_flag_accepted_without_error(self):
        """screenshot(full_page=True) must not raise — it's silently ignored for MVP."""
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = [
            {"data": self._make_png_b64()},
            self._target_info_reply(),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            screenshot(full_page=True)  # must not raise

    def test_returns_image_and_metadata_pair(self):
        """Driving test (#41.3): screenshot() must return [Image, metadata]
        where metadata carries width/height (from the PNG's own IHDR chunk,
        not a Page.getLayoutMetrics round-trip) plus url/title from
        Target.getTargetInfo."""
        engine = _fake_engine_with_session()
        png_bytes = self._make_well_formed_png_bytes(10, 20)
        engine.session.send.side_effect = [
            {"data": base64.b64encode(png_bytes).decode()},
            self._target_info_reply(url="http://example.test", title="Example"),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        # Unpacking a bare Image (today's return shape) raises TypeError —
        # that is the expected RED for this driving test.
        image, metadata = result

        assert isinstance(image, Image)
        assert metadata == {
            "width": 10,
            "height": 20,
            "url": "http://example.test",
            "title": "Example",
        }

    def test_does_not_call_get_layout_metrics(self):
        """screenshot() must source width/height from the decoded PNG bytes,
        never from a Page.getLayoutMetrics CDP round-trip (#41.3)."""
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = [
            {"data": base64.b64encode(self._make_well_formed_png_bytes(1, 1)).decode()},
            self._target_info_reply(),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            screenshot()

        methods_called = [c[0][0] for c in engine.session.send.call_args_list]
        assert "Page.getLayoutMetrics" not in methods_called

    def test_metadata_url_and_title_from_target_info(self):
        """screenshot()'s metadata url/title must come from Target.getTargetInfo,
        read the same way get_page_info() reads it (#41.3)."""
        engine = _fake_engine_with_session()
        engine.session.send.side_effect = [
            {"data": self._make_png_b64()},
            self._target_info_reply(url="http://other.test", title="Other"),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        _, metadata = result
        assert metadata["url"] == "http://other.test"
        assert metadata["title"] == "Other"

    def test_second_png_size_and_second_call_method_name_via_screenshot(self):
        """#41.2 test-critic gap: the only PNG-size case previously driven
        through screenshot() itself was 10x20 — a hardcoded width/height
        literal in screenshot() could still coincidentally satisfy that one
        case. Drive a second, distinct size (640x480, matching the
        TestPngDimensions unit-test fixture) through screenshot() itself so
        two different literal values would have to both be hardcoded to
        fake a pass. Also assert the second CDP call's method name is
        literally "Target.getTargetInfo", not just that url/title appear in
        the result."""
        engine = _fake_engine_with_session()
        png_bytes = self._make_well_formed_png_bytes(640, 480)
        engine.session.send.side_effect = [
            {"data": base64.b64encode(png_bytes).decode()},
            self._target_info_reply(url="http://example.test", title="Example"),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()

        _, metadata = result
        assert metadata["width"] == 640
        assert metadata["height"] == 480

        second_call = engine.session.send.call_args_list[1]
        assert second_call[0][0] == "Target.getTargetInfo"

    def test_target_info_failure_does_not_lose_captured_screenshot(self):
        """Review fix (#42): if Target.getTargetInfo fails after the
        screenshot bytes were already captured and decoded (e.g. the tab
        closed or navigated away between the two CDP calls), screenshot()
        must not lose the already-successfully-captured image — it must
        still return [Image, metadata] with url/title falling back to None,
        rather than propagating the exception."""
        engine = _fake_engine_with_session()
        png_bytes = self._make_well_formed_png_bytes(10, 20)
        engine.session.send.side_effect = [
            {"data": base64.b64encode(png_bytes).decode()},
            RuntimeError("target closed"),
        ]

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = screenshot()  # must not raise

        image, metadata = result
        assert isinstance(image, Image)
        assert metadata == {
            "width": 10,
            "height": 20,
            "url": None,
            "title": None,
        }


class TestPngDimensions:
    """Tests for the private _png_dimensions() helper (#41.3)."""

    def test_parses_width_and_height_from_ihdr(self):
        """_png_dimensions() must read width/height as big-endian uint32 from
        the IHDR chunk at bytes [16:20]/[20:24]."""
        from chrome_wrapper_plugin.server import _png_dimensions

        header = (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + (640).to_bytes(4, "big")
            + (480).to_bytes(4, "big")
        )
        assert _png_dimensions(header) == (640, 480)

    def test_returns_none_none_for_non_png_bytes(self):
        """_png_dimensions() must return (None, None) for bytes that don't
        start with the PNG signature."""
        from chrome_wrapper_plugin.server import _png_dimensions

        assert _png_dimensions(b"not a png at all") == (None, None)

    def test_returns_none_none_for_wrong_signature_at_full_length(self):
        """A buffer that is long enough (>=24 bytes) to contain a full IHDR
        chunk, but whose leading 8 bytes are not the PNG signature, must
        still return (None, None) — this exercises the signature check
        itself rather than the length-guard short-circuiting it."""
        from chrome_wrapper_plugin.server import _png_dimensions

        buf = b"NOTAPNG!" + (13).to_bytes(4, "big") + b"IHDR" + (1).to_bytes(4, "big") * 2
        assert len(buf) >= 24
        assert _png_dimensions(buf) == (None, None)

    def test_returns_none_none_for_wrong_ihdr_tag_at_full_length(self):
        """A buffer with a valid PNG signature and correct length, but whose
        chunk-type bytes at [12:16] are not "IHDR", must return (None, None)
        — this exercises the IHDR-tag check itself."""
        from chrome_wrapper_plugin.server import _png_dimensions

        buf = (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")
            + b"XXXX"
            + (1).to_bytes(4, "big") * 2
        )
        assert len(buf) >= 24
        assert _png_dimensions(buf) == (None, None)

    def test_returns_none_none_for_truncated_signature_only(self):
        """_png_dimensions() must not raise on a signature-only buffer with no
        IHDR chunk (len < 24) — it must return (None, None) instead."""
        from chrome_wrapper_plugin.server import _png_dimensions

        assert _png_dimensions(b"\x89PNG\r\n\x1a\n") == (None, None)


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


# ── _resolve_element_center ───────────────────────────────────────────────────

class TestResolveElementCenter:
    """Tests for the _resolve_element_center() helper."""

    def test_returns_coordinates_from_js_result(self):
        """_resolve_element_center() must return (x, y) floats from the JS result."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": {"x": 100.0, "y": 200.0}}
        }

        result = _resolve_element_center(engine.session, "#btn")

        assert result == (100.0, 200.0)

    def test_raises_value_error_when_no_element(self):
        """_resolve_element_center() must raise ValueError when JS returns null."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": None}}

        with pytest.raises(ValueError, match="No element found"):
            _resolve_element_center(engine.session, "#missing")

    def test_raises_runtime_error_on_exception_details(self):
        """_resolve_element_center() must raise RuntimeError when CDP returns exceptionDetails."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "exceptionDetails": {"text": "boom"},
            "result": {},
        }

        with pytest.raises(RuntimeError):
            _resolve_element_center(engine.session, "#bad")

    def test_sends_runtime_evaluate_with_selector(self):
        """_resolve_element_center() must call Runtime.evaluate with an expression containing the selector."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": {"x": 50.0, "y": 60.0}}
        }

        _resolve_element_center(engine.session, "#my-btn")

        call_args = engine.session.send.call_args
        assert call_args[0][0] == "Runtime.evaluate"
        assert "#my-btn" in call_args[0][1]["expression"]


# ── get_element_info ─────────────────────────────────────────────────────────
#
# get_element_info() does not exist yet (#41.1) — every test below imports it
# locally inside the test body (mirroring tests/test_fastmcp_client.py's
# documented discipline) so a missing symbol surfaces as a named failing test
# for *this* class only, rather than an ImportError that breaks collection of
# every other test in this file.

class TestGetElementInfo:
    """Tests for the get_element_info(selector) tool (#41.1)."""

    _MISSING_ELEMENT_VALUE = {
        "exists": False,
        "visible": False,
        "text": None,
        "value": None,
        "checked": None,
        "rect": None,
    }

    def test_returns_exactly_the_six_fields(self):
        """Driving test: get_element_info() must return exactly the six
        documented keys — exists, visible, text, value, checked, rect — AND
        the CDP reply's actual values must flow through unchanged, so a
        hardcoded/discarded-reply implementation cannot pass."""
        from chrome_wrapper_plugin.server import get_element_info

        cdp_value = {
            "exists": True,
            "visible": True,
            "text": "hello",
            "value": "abc",
            "checked": None,
            "rect": {
                "x": 1, "y": 2, "width": 3, "height": 4,
                "top": 2, "right": 4, "bottom": 6, "left": 1,
            },
        }
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": cdp_value}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_element_info("#name")

        assert set(result.keys()) == {
            "exists", "visible", "text", "value", "checked", "rect",
        }
        assert result == cdp_value

    def test_missing_element_returns_all_none_or_false(self):
        """A selector that matches nothing is a normal answer, not an error:
        exists/visible are False and the rest are None."""
        from chrome_wrapper_plugin.server import get_element_info

        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": dict(self._MISSING_ELEMENT_VALUE)}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = get_element_info("#missing")

        assert result == self._MISSING_ELEMENT_VALUE

    def test_expression_interpolates_selector(self):
        """The selector must be interpolated into the JS expression, same
        style as _resolve_element_center/wait_for_selector."""
        from chrome_wrapper_plugin.server import get_element_info

        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": dict(self._MISSING_ELEMENT_VALUE)}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            get_element_info("#foo-bar")

        expr = engine.session.send.call_args[0][1]["expression"]
        assert "#foo-bar" in expr

    def test_expression_reuses_visibility_predicate_and_truncates_text_in_js(self):
        """The expression must reuse wait_for_selector's visible predicate
        (not display:none, not visibility:hidden) and truncate innerText to
        2000 chars in JS, so an oversized string never crosses the wire."""
        from chrome_wrapper_plugin.server import get_element_info

        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": dict(self._MISSING_ELEMENT_VALUE)}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            get_element_info("#foo")

        expr = engine.session.send.call_args[0][1]["expression"]
        assert "display" in expr and "none" in expr
        assert "visibility" in expr and "hidden" in expr
        assert "innerText" in expr
        assert "2000" in expr and "slice" in expr

    def test_expression_builds_rect_fields_explicitly(self):
        """rect must be built as an explicit plain object from
        getBoundingClientRect() — returnByValue serialises a raw DOMRect to
        {} because its properties are prototype getters, not own props."""
        from chrome_wrapper_plugin.server import get_element_info

        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": dict(self._MISSING_ELEMENT_VALUE)}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            get_element_info("#foo")

        expr = engine.session.send.call_args[0][1]["expression"]
        assert "getBoundingClientRect" in expr
        for field in ("x", "y", "width", "height", "top", "right", "bottom", "left"):
            assert field in expr, f"rect field {field!r} not built explicitly in {expr!r}"

    def test_expression_does_not_call_scroll_into_view(self):
        """Unlike _resolve_element_center, get_element_info() must not scroll
        the element into view — inspection must not have a side effect that
        changes the rect it reports."""
        from chrome_wrapper_plugin.server import get_element_info

        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": dict(self._MISSING_ELEMENT_VALUE)}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            get_element_info("#foo")

        expr = engine.session.send.call_args[0][1]["expression"]
        assert "scrollIntoView" not in expr

    def test_raises_runtime_error_on_exception_details(self):
        """get_element_info() must raise RuntimeError when CDP Runtime.evaluate
        returns exceptionDetails, matching fill()/select_option()."""
        from chrome_wrapper_plugin.server import get_element_info

        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "exceptionDetails": {"text": "syntax error"},
            "result": {},
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            with pytest.raises(RuntimeError):
                get_element_info("#foo")

    def test_sends_exactly_one_runtime_evaluate_call(self):
        """get_element_info() must resolve everything in a single CDP
        round-trip — one Runtime.evaluate call, nothing more."""
        from chrome_wrapper_plugin.server import get_element_info

        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": dict(self._MISSING_ELEMENT_VALUE)}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            get_element_info("#foo")

        assert engine.session.send.call_count == 1
        assert engine.session.send.call_args[0][0] == "Runtime.evaluate"


# ── click ─────────────────────────────────────────────────────────────────────

class TestClick:
    """Tests for the click() tool."""

    def test_click_resolves_and_dispatches_three_mouse_events(self):
        """click() must dispatch mouseMoved, mousePressed, mouseReleased."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(50.0, 75.0)):
            click("#btn")

        calls = engine.session.send.call_args_list
        assert len(calls) == 3
        event_types = [c[0][1]["type"] for c in calls]
        assert event_types == ["mouseMoved", "mousePressed", "mouseReleased"]

    def test_click_returns_coordinates(self):
        """click() must return {"x": float, "y": float}."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(50.0, 75.0)):
            result = click("#btn")

        assert result == {"x": 50.0, "y": 75.0}

    def test_click_button_left_on_press_and_release(self):
        """click() must use button='left' for mousePressed/mouseReleased and button='none' for mouseMoved."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(50.0, 75.0)):
            click("#btn")

        calls = engine.session.send.call_args_list
        move_params = calls[0][0][1]
        press_params = calls[1][0][1]
        release_params = calls[2][0][1]
        assert move_params["button"] == "none"
        assert press_params["button"] == "left"
        assert release_params["button"] == "left"

    def test_click_raises_value_error_when_selector_not_found(self):
        """click() must propagate ValueError from _resolve_element_center."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", side_effect=ValueError("No element found")):
            with pytest.raises(ValueError, match="No element found"):
                click("#missing")


# ── hover ─────────────────────────────────────────────────────────────────────

class TestHover:
    """Tests for the hover() tool."""

    def test_hover_dispatches_mouse_moved_only(self):
        """hover() must dispatch exactly one Input.dispatchMouseEvent with type=mouseMoved."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(30.0, 40.0)):
            hover("#link")

        calls = engine.session.send.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "Input.dispatchMouseEvent"
        assert calls[0][0][1]["type"] == "mouseMoved"

    def test_hover_returns_coordinates(self):
        """hover() must return {"x": float, "y": float}."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(30.0, 40.0)):
            result = hover("#link")

        assert result == {"x": 30.0, "y": 40.0}

    def test_hover_raises_value_error_when_selector_not_found(self):
        """hover() must propagate ValueError from _resolve_element_center."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", side_effect=ValueError("No element found")):
            with pytest.raises(ValueError, match="No element found"):
                hover("#missing")


# ── type ──────────────────────────────────────────────────────────────────────

class TestType:
    """Tests for the type() tool (imported as type_text)."""

    def test_type_clicks_then_dispatches_key_events(self):
        """type() with 'ab' must send 3 mouse events then 4 key events (keyDown+keyUp per char)."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(10.0, 20.0)):
            type_text("#input", "ab")

        calls = engine.session.send.call_args_list
        # 3 mouse events + 2 chars * 2 key events = 7 total
        assert len(calls) == 7
        mouse_calls = [c for c in calls if c[0][0] == "Input.dispatchMouseEvent"]
        key_calls = [c for c in calls if c[0][0] == "Input.dispatchKeyEvent"]
        assert len(mouse_calls) == 3
        assert len(key_calls) == 4

    def test_type_key_events_carry_text_and_unmodified_text(self):
        """type() key events must have text and unmodifiedText set to the character."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(10.0, 20.0)):
            type_text("#input", "a")

        key_calls = [c for c in engine.session.send.call_args_list
                     if c[0][0] == "Input.dispatchKeyEvent"]
        assert len(key_calls) == 2
        for call in key_calls:
            params = call[0][1]
            assert params["text"] == "a"
            assert params["unmodifiedText"] == "a"

    def test_type_returns_character_count(self):
        """type() must return {"typed": len(text)}."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(10.0, 20.0)):
            result = type_text("#input", "hello")

        assert result == {"typed": 5}

    def test_type_empty_string_sends_only_click_events(self):
        """type() with empty string must send 3 mouse events and 0 key events."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", return_value=(10.0, 20.0)):
            result = type_text("#input", "")

        calls = engine.session.send.call_args_list
        mouse_calls = [c for c in calls if c[0][0] == "Input.dispatchMouseEvent"]
        key_calls = [c for c in calls if c[0][0] == "Input.dispatchKeyEvent"]
        assert len(mouse_calls) == 3
        assert len(key_calls) == 0
        assert result == {"typed": 0}

    def test_type_raises_value_error_when_selector_not_found(self):
        """type() must propagate ValueError from _resolve_element_center."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", side_effect=ValueError("No element found")):
            with pytest.raises(ValueError, match="No element found"):
                type_text("#missing", "hello")

    def test_docstring_cross_references_fill_focus_behavior(self):
        """#40: type()'s docstring must cross-reference that fill() also now
        leaves the element focused, so a caller knows both tools compose with
        a follow-up press_key("Enter"). Requires both "fill" and "focus" to
        appear, so a docstring that already said "fill" for unrelated
        reasons (e.g. "form-fill") can't pass vacuously without actually
        documenting the focus behaviour."""
        doc = type_text.__doc__.lower()
        assert "fill" in doc, type_text.__doc__
        assert "focus" in doc, type_text.__doc__


# ── fill ──────────────────────────────────────────────────────────────────────

class TestFill:
    """Tests for the fill() tool."""

    def test_fill_calls_resolve_then_runtime_evaluate(self):
        """fill() must call _resolve_element_center and then Runtime.evaluate with selector and value."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": True}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center") as mock_resolve:
            fill("#name", "Alice")

        mock_resolve.assert_called_once_with(engine.session, "#name")
        call_args = engine.session.send.call_args
        assert call_args[0][0] == "Runtime.evaluate"
        expr = call_args[0][1]["expression"]
        assert "#name" in expr
        assert "Alice" in expr

    def test_fill_returns_filled_true(self):
        """fill() must return {"filled": True} on success."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": True}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            result = fill("#name", "Alice")

        assert result == {"filled": True}

    def test_fill_raises_runtime_error_on_exception_details(self):
        """fill() must raise RuntimeError when CDP Runtime.evaluate returns exceptionDetails."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "exceptionDetails": {"text": "syntax error"},
            "result": {},
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            with pytest.raises(RuntimeError):
                fill("#name", "Alice")

    def test_fill_raises_value_error_when_selector_not_found(self):
        """fill() must propagate ValueError from _resolve_element_center."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center", side_effect=ValueError("No element found")):
            with pytest.raises(ValueError, match="No element found"):
                fill("#missing", "value")

    def test_fill_expression_focuses_before_setting_value(self):
        """Driving test (#40): fill() must call el.focus() before setting the
        value, so a follow-up press_key("Enter") targets the filled element —
        this is the fill()/type() focus asymmetry the ticket reports."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": True}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            fill("#name", "Alice")

        expr = engine.session.send.call_args[0][1]["expression"]
        assert "el.focus()" in expr, expr

        focus_index = expr.index("el.focus()")
        value_set_indices = [
            i for i in (
                expr.find("nativeInputValueSetter.call(el,"),
                expr.find("el.value ="),
            )
            if i != -1
        ]
        assert value_set_indices, expr
        assert all(focus_index < i for i in value_set_indices), expr

    def test_fill_docstring_mentions_focus(self):
        """#40: fill()'s docstring must state that it focuses the element, so
        a caller knows a follow-up press_key("Enter") will target it. Checks
        for a phrase naming the concrete behaviour ("focuses the element")
        rather than a bare "focus" substring match, so an unrelated sentence
        that happens to contain the word "focus" can't satisfy this
        vacuously."""
        doc = fill.__doc__.lower()
        assert "focus" in doc, fill.__doc__
        assert "focuses the element" in doc, fill.__doc__


# ── press_key ─────────────────────────────────────────────────────────────────

class TestPressKey:
    """Tests for the press_key() tool."""

    def test_press_key_dispatches_key_down_then_key_up(self):
        """press_key() must dispatch keyDown then keyUp with the given key."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            press_key("Enter")

        calls = engine.session.send.call_args_list
        assert len(calls) == 2
        assert calls[0][0][0] == "Input.dispatchKeyEvent"
        assert calls[0][0][1]["type"] == "keyDown"
        assert calls[0][0][1]["key"] == "Enter"
        assert calls[1][0][0] == "Input.dispatchKeyEvent"
        assert calls[1][0][1]["type"] == "keyUp"
        assert calls[1][0][1]["key"] == "Enter"

    def test_press_key_returns_key_name(self):
        """press_key() must return {"key": key}."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = press_key("Enter")

        assert result == {"key": "Enter"}

    def test_press_key_works_without_selector(self):
        """press_key() must not call Runtime.evaluate — no selector resolution needed."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            press_key("Tab")

        # Only Input.dispatchKeyEvent calls, no Runtime.evaluate
        for call in engine.session.send.call_args_list:
            assert call[0][0] == "Input.dispatchKeyEvent"


# ── select_option ─────────────────────────────────────────────────────────────

class TestSelectOption:
    """Tests for the select_option() tool."""

    def test_select_option_dispatches_runtime_evaluate(self):
        """select_option() must call Runtime.evaluate with an expression containing selector and value."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": {"ok": True}}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            select_option("#color", "blue")

        call_args = engine.session.send.call_args
        assert call_args[0][0] == "Runtime.evaluate"
        expr = call_args[0][1]["expression"]
        assert "#color" in expr
        assert "blue" in expr

    def test_select_option_returns_selected_value(self):
        """select_option() must return {"selected": value} on success."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": {"ok": True}}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            result = select_option("#color", "blue")

        assert result == {"selected": "blue"}

    def test_select_option_raises_value_error_on_not_found(self):
        """select_option() must raise ValueError when JS reports reason='not_found'."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": {"ok": False, "reason": "not_found"}}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            with pytest.raises(ValueError, match="No element found"):
                select_option("#missing", "blue")

    def test_select_option_raises_value_error_on_invalid_value(self):
        """select_option() must raise ValueError when JS reports reason='invalid_value'."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "result": {"value": {"ok": False, "reason": "invalid_value", "options": ["a", "b"]}}
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            with pytest.raises(ValueError, match="not in options"):
                select_option("#color", "blue")

    def test_select_option_raises_runtime_error_on_exception_details(self):
        """select_option() must raise RuntimeError when CDP returns exceptionDetails."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "exceptionDetails": {"text": "some error"},
            "result": {},
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_resolve_element_center"):
            with pytest.raises(RuntimeError):
                select_option("#color", "blue")


# ── wait_for_selector ─────────────────────────────────────────────────────────

class TestWaitForSelector:
    """Tests for the wait_for_selector() tool."""

    def test_resolves_attached_when_element_present(self):
        """wait_for_selector() returns correct dict when element is immediately in DOM."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": True}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = wait_for_selector("#btn", state="attached")

        assert result["selector"] == "#btn"
        assert result["state"] == "attached"
        assert "elapsed" in result

    def test_resolves_visible_when_element_visible(self):
        """wait_for_selector() returns correct dict when element is immediately visible."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": True}}

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = wait_for_selector("#btn", state="visible")

        assert result["selector"] == "#btn"
        assert result["state"] == "visible"
        assert "elapsed" in result

    def test_raises_timeout_error_when_element_never_appears(self):
        """wait_for_selector() raises TimeoutError when element is never found within timeout."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {"result": {"value": False}}

        # The implementation calls time.monotonic() in this order (after the nit fix
        # where start and deadline share a single monotonic call):
        #   1. start = time.monotonic()                → return T  (deadline = T + timeout)
        #   (loop) session.send → False
        #   2. remaining = deadline - time.monotonic() → return T + timeout + 1 → remaining = -1 ≤ 0
        # StopIteration would surface as an exception if the list is exhausted, so provide
        # a few extra values as a guard — they should never be consumed.
        T = 1000.0
        timeout = 5.0
        monotonic_values = [T, T + timeout + 1, T + timeout + 2, T + timeout + 3]

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch("chrome_wrapper_plugin.server.time.monotonic",
                        side_effect=monotonic_values), \
             mock.patch("chrome_wrapper_plugin.server.time.sleep"):
            with pytest.raises(TimeoutError, match="timed out"):
                wait_for_selector("#btn", timeout=timeout, state="attached")

    def test_raises_value_error_for_unknown_state(self):
        """wait_for_selector() raises ValueError for state other than 'attached'/'visible'.

        The guard fires before _get_engine() is called, so no engine is ever
        constructed.  We patch _get_engine with a side_effect that raises
        AssertionError to make the test fail if the guard is ever bypassed.
        """
        guard = mock.MagicMock(side_effect=AssertionError("engine should not be constructed"))
        with mock.patch.object(server_module, "_get_engine", guard):
            with pytest.raises(ValueError, match="state="):
                wait_for_selector("#x", state="detached")

        guard.assert_not_called()

    def test_raises_runtime_error_on_js_exception(self):
        """wait_for_selector() raises RuntimeError when Runtime.evaluate returns exceptionDetails."""
        engine = _fake_engine_with_session()
        engine.session.send.return_value = {
            "exceptionDetails": {"text": "boom"},
            "result": {},
        }

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            with pytest.raises(RuntimeError, match="JS exception"):
                wait_for_selector("#btn")


# ── wait_for_navigation ───────────────────────────────────────────────────────

class TestWaitForNavigation:
    """Tests for the wait_for_navigation() tool."""

    def test_resolves_on_load_event(self):
        """wait_for_navigation() returns {"event": "Page.loadEventFired"} when load fires."""
        engine = _fake_engine_with_session()

        def _instant_listener(event_name, cb):
            if event_name == "Page.loadEventFired":
                cb({})

        engine.session.add_listener.side_effect = _instant_listener

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = wait_for_navigation()

        assert result == {"event": "Page.loadEventFired"}

    def test_raises_timeout_error_when_no_load_event(self):
        """wait_for_navigation() raises TimeoutError when Page.loadEventFired never fires."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(threading.Event, "wait", return_value=False):
            with pytest.raises(TimeoutError, match="Page.loadEventFired not received"):
                wait_for_navigation(timeout=5.0)

    def test_removes_listener_in_finally(self):
        """wait_for_navigation() always removes the Page.loadEventFired listener, even on TimeoutError."""
        engine = _fake_engine_with_session()
        captured_cb: dict = {}

        def _capture_listener(event_name, cb):
            if event_name == "Page.loadEventFired":
                captured_cb["fn"] = cb

        engine.session.add_listener.side_effect = _capture_listener

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(threading.Event, "wait", return_value=False):
            with pytest.raises(TimeoutError):
                wait_for_navigation(timeout=5.0)

        engine.session.remove_listener.assert_called_once_with(
            "Page.loadEventFired", captured_cb["fn"]
        )


# ── wait_for_network_idle ─────────────────────────────────────────────────────

class TestWaitForNetworkIdle:
    """Tests for the wait_for_network_idle() tool."""

    def test_resolves_immediately_when_already_idle(self):
        """wait_for_network_idle() returns the full networkIdle payload —
        event, requests_seen, in_flight_after_settle, elapsed — when no
        requests are in flight (#41.2). `elapsed` must be bounded below by
        the real settle window (which is not mocked here, so a genuine
        time.sleep must occur) and bounded above generously, so a
        hardcoded literal like 0.0 cannot pass."""
        engine = _fake_engine_with_session()
        wall_start = time.monotonic()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = wait_for_network_idle()

        wall_elapsed = time.monotonic() - wall_start

        assert result["event"] == "networkIdle"
        assert result["requests_seen"] == 0
        assert result["in_flight_after_settle"] == 0
        assert isinstance(result["elapsed"], float)
        assert result["elapsed"] >= server_module._NETWORK_IDLE_SETTLE * 0.8, result
        assert result["elapsed"] <= wall_elapsed + 0.05, (result, wall_elapsed)

    def test_sends_time_sleep_for_settle_window(self):
        """Driving test (#41.2): wait_for_network_idle() must sleep for a
        settle window (to catch late-arriving requests) before evaluating
        whether the network is idle. Today it never calls time.sleep at all."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch("chrome_wrapper_plugin.server.time.sleep") as mock_sleep:
            wait_for_network_idle()

        mock_sleep.assert_called_once()

    def test_returns_four_key_networkidle_payload(self):
        """Driving test (#41.2): the return dict must carry exactly
        event/elapsed/requests_seen/in_flight_after_settle — today it is only
        {"event": "networkIdle"}, so requests_seen is missing."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            result = wait_for_network_idle()

        assert set(result.keys()) == {
            "event", "elapsed", "requests_seen", "in_flight_after_settle",
        }

    def test_default_settle_constant_is_point_three_seconds(self):
        """#41.2: the settle window defaults to 0.3s, exposed as a
        module-level constant so tests can monkeypatch it small."""
        assert server_module._NETWORK_IDLE_SETTLE == 0.3

    def test_settle_sleep_is_clamped_to_timeout(self):
        """#41.2: when timeout is shorter than the settle window, the settle
        sleep must be clamped to timeout, never overrun the caller's budget."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch("chrome_wrapper_plugin.server.time.sleep") as mock_sleep:
            wait_for_network_idle(timeout=0.05)

        mock_sleep.assert_called_once_with(
            min(server_module._NETWORK_IDLE_SETTLE, 0.05)
        )

    def test_settle_window_actually_elapses_wall_clock_time(self):
        """#41.2: the settle window must be a real time.sleep, not
        threading.Event.wait — the existing timeout tests below monkeypatch
        threading.Event.wait globally to return False, which would swallow an
        Event.wait-based settle instead of actually delaying. Event.wait is
        mocked to return True here (fires immediately) so the *only* possible
        source of wall-clock delay is the settle sleep itself."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_NETWORK_IDLE_SETTLE", 0.05), \
             mock.patch.object(threading.Event, "wait", return_value=True):
            start = time.monotonic()
            wait_for_network_idle()
            elapsed = time.monotonic() - start

        assert elapsed >= 0.05, elapsed

    def test_requests_seen_counts_total_requests_not_just_in_flight(self):
        """#41.2: requests_seen must be a monotonically increasing total
        count, unlike the in-flight counter which decrements back to 0."""
        import threading as _threading

        engine = _fake_engine_with_session()
        captured: dict = {}
        listeners_ready = _threading.Event()

        def _capture_listener(event_name, cb):
            captured[event_name] = cb
            if len(captured) == 3:
                # Two requests arrive; only the first is settled here — the
                # second is completed from the main thread below, mirroring
                # test_resolves_after_request_completes's synchronisation.
                captured["Network.requestWillBeSent"]({})
                captured["Network.requestWillBeSent"]({})
                captured["Network.loadingFinished"]({})
                listeners_ready.set()

        engine.session.add_listener.side_effect = _capture_listener

        result_holder: dict = {}
        error_holder: dict = {}

        def _run():
            try:
                result_holder["r"] = wait_for_network_idle()
            except Exception as e:  # noqa: BLE001 - surfaced via error_holder
                error_holder["e"] = e

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            t = _threading.Thread(target=_run)
            t.start()

            assert listeners_ready.wait(timeout=5.0), "Listeners never registered"
            captured["Network.loadingFinished"]({})  # second request completes

            t.join(timeout=5.0)

        assert not t.is_alive(), "Tool thread did not finish — possible deadlock"
        assert not error_holder, f"Unexpected error: {error_holder}"
        assert result_holder["r"]["requests_seen"] == 2

    def test_resolves_after_request_completes(self):
        """wait_for_network_idle() resolves once an in-flight request finishes.

        This test exercises the counter path, NOT the pre-set fast-path.

        Strategy: fire _on_request({}) from inside the add_listener side_effect
        (while all three listeners are being registered) so that _in_flight
        becomes 1 before the guarded pre-set runs.  The guarded pre-set then
        sees _in_flight[0] > 0 and leaves idle_event cleared.  The tool blocks
        on idle_event.wait().  The main thread then fires _on_done({}) which
        decrements _in_flight to 0 and sets idle_event, unblocking the tool.
        """
        import threading as _threading

        engine = _fake_engine_with_session()
        captured: dict = {}
        # Synchronise: unblocked once all three listeners are registered AND
        # _on_request has been fired (inside the side_effect), so _in_flight==1.
        listeners_ready = _threading.Event()

        def _capture_listener(event_name, cb):
            captured[event_name] = cb
            # After all three listeners are captured, fire _on_request so
            # _in_flight becomes 1 before the guarded pre-set executes.
            if len(captured) == 3:
                captured["Network.requestWillBeSent"]({})
                listeners_ready.set()

        engine.session.add_listener.side_effect = _capture_listener

        result_holder: dict = {}
        error_holder: dict = {}

        def _run():
            try:
                result_holder["r"] = wait_for_network_idle()
            except Exception as e:
                error_holder["e"] = e

        with mock.patch.object(server_module, "_get_engine", return_value=engine):
            t = _threading.Thread(target=_run)
            t.start()

            # Wait until _on_request has been fired (in_flight==1, event cleared).
            assert listeners_ready.wait(timeout=5.0), "Listeners never registered"

            # Now fire _on_done: decrements in_flight to 0 and sets idle_event.
            captured["Network.loadingFinished"]({})

            t.join(timeout=5.0)

        assert not t.is_alive(), "Tool thread did not finish — possible deadlock"
        assert not error_holder, f"Unexpected error: {error_holder}"
        result = result_holder.get("r")
        assert result["event"] == "networkIdle"
        assert result["requests_seen"] == 1
        assert result["in_flight_after_settle"] == 0

    def test_in_flight_after_settle_is_nonzero_when_request_still_pending(self):
        """#41.2: in_flight_after_settle must report the live counter, not a
        hardcoded 0. Fires a request that is still in flight when the
        (shortened) settle window elapses, then completes it afterward so
        the call still resolves normally — the request only finishes
        *after* the settle re-evaluation has already captured in_flight==1.
        """
        import threading as _threading

        engine = _fake_engine_with_session()
        captured: dict = {}
        listeners_ready = _threading.Event()

        def _capture_listener(event_name, cb):
            captured[event_name] = cb
            if len(captured) == 3:
                # Request arrives before the settle sleep starts, and is
                # deliberately left in-flight — not completed here.
                captured["Network.requestWillBeSent"]({})
                listeners_ready.set()

        engine.session.add_listener.side_effect = _capture_listener

        result_holder: dict = {}
        error_holder: dict = {}

        def _run():
            try:
                result_holder["r"] = wait_for_network_idle()
            except Exception as e:  # noqa: BLE001 - surfaced via error_holder
                error_holder["e"] = e

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(server_module, "_NETWORK_IDLE_SETTLE", 0.05):
            t = _threading.Thread(target=_run)
            t.start()

            assert listeners_ready.wait(timeout=5.0), "Listeners never registered"
            # Wait past the (shortened) settle window while the request is
            # still pending, so the post-settle re-evaluation captures
            # in_flight_after_settle == 1, then complete it.
            time.sleep(0.2)
            captured["Network.loadingFinished"]({})

            t.join(timeout=5.0)

        assert not t.is_alive(), "Tool thread did not finish — possible deadlock"
        assert not error_holder, f"Unexpected error: {error_holder}"
        result = result_holder.get("r")
        assert result["event"] == "networkIdle"
        assert result["in_flight_after_settle"] == 1, result

    def test_raises_timeout_error_when_requests_never_complete(self):
        """wait_for_network_idle() raises TimeoutError when network never goes idle."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(threading.Event, "wait", return_value=False):
            with pytest.raises(TimeoutError, match="Network did not go idle"):
                wait_for_network_idle(timeout=5.0)

    def test_removes_all_three_listeners_in_finally(self):
        """wait_for_network_idle() always removes all three Network listeners, even on TimeoutError."""
        engine = _fake_engine_with_session()

        with mock.patch.object(server_module, "_get_engine", return_value=engine), \
             mock.patch.object(threading.Event, "wait", return_value=False):
            with pytest.raises(TimeoutError):
                wait_for_network_idle(timeout=5.0)

        remove_calls = engine.session.remove_listener.call_args_list
        removed_events = {call[0][0] for call in remove_calls}
        assert removed_events == {
            "Network.requestWillBeSent",
            "Network.loadingFinished",
            "Network.loadingFailed",
        }
        assert len(remove_calls) == 3


# ── sleep ─────────────────────────────────────────────────────────────────────

class TestSleep:
    """Tests for the sleep() tool."""

    def test_sleep_calls_time_sleep_and_returns_slept(self):
        """sleep() must call time.sleep with the given duration and return {"slept": seconds}."""
        with mock.patch("chrome_wrapper_plugin.server.time.sleep") as mock_sleep:
            result = sleep_tool(2.5)

        mock_sleep.assert_called_once_with(2.5)
        assert result == {"slept": 2.5}

    def test_sleep_raises_value_error_for_negative(self):
        """sleep() must raise ValueError when seconds is negative."""
        with pytest.raises(ValueError, match="between 0 and 60"):
            sleep_tool(-1)

    def test_sleep_raises_value_error_above_max(self):
        """sleep() must raise ValueError when seconds exceeds 60."""
        with pytest.raises(ValueError, match="between 0 and 60"):
            sleep_tool(61)
