"""Behavioural coverage for the #26 migration from the FastMCP class vendored
inside `mcp<2` (module path: mcp . server . fastmcp) to the standalone
`fastmcp>=2,<3` package.

This file must not import `chrome_wrapper_plugin` or `fastmcp` at module
scope — every such import happens inside a test body so a broken/mid-
migration environment surfaces as a named failing test, not a bare
collection error (same discipline as tests/test_dependencies.py).

The expected tool-name set (R2) is derived independently via `ast` parsing
of server.py's source rather than hand-typed here, to avoid sharing the
authorship blind spot of a hand-typed list against the `@mcp.tool()`
registration block itself.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "src" / "chrome_wrapper_plugin" / "server.py"


def _ast_derived_tool_names() -> set[str]:
    """Every module-level def/async def in server.py, minus `_`-prefixed
    helpers and `main` — this selects precisely the `@mcp.tool()`-decorated
    functions without re-typing their names by hand."""
    source = SERVER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SERVER_PATH))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") or node.name == "main":
                continue
            names.add(node.name)
    return names


def _read_content_blocks(result):
    """Return the list of content blocks from a tool-call result, tolerating
    both a `CallToolResult.content` attribute and a bare list return shape
    (exact fastmcp 2.x point-release shape is unconfirmed)."""
    content = getattr(result, "content", None)
    if content is not None:
        return content
    if isinstance(result, list):
        return result
    raise AssertionError(f"cannot extract content blocks from {result!r}")


def _list_tools_sync():
    """Run fastmcp.Client(server_module.mcp).list_tools() via asyncio.run,
    following the tests/test_server.py asyncio.run(_run()) pattern."""
    import asyncio

    import fastmcp

    import chrome_wrapper_plugin.server as server_module

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.list_tools()

    return asyncio.run(_run())


# ── R1: server.mcp must be a real fastmcp.FastMCP instance ────────────────

def test_server_mcp_is_fastmcp_2x_instance():
    """`chrome_wrapper_plugin.server.mcp` must be built from the standalone
    `fastmcp.FastMCP` class, not the FastMCP class vendored inside `mcp<2`.

    This is the one deterministic authoring-time RED: it fails today
    regardless of whether fastmcp.Client happens to duck-type-accept the
    1.x server object (it does — see R2/R3/R4), because `server.mcp` is
    still constructed from the vendored 1.x class.
    """
    import fastmcp

    import chrome_wrapper_plugin.server as server_module

    assert isinstance(server_module.mcp, fastmcp.FastMCP), (
        f"server_module.mcp is {type(server_module.mcp)!r}, not an "
        "instance of fastmcp.FastMCP (the standalone package) — server.py "
        "still builds `mcp` from the vendored 1.x FastMCP class"
    )
    assert type(server_module.mcp).__module__.split(".")[0] == "fastmcp", (
        f"type(server_module.mcp).__module__ is "
        f"{type(server_module.mcp).__module__!r}, not rooted at the "
        "standalone `fastmcp` package — an isinstance-only check can be "
        "fooled by a subclass registered under the vendored module path"
    )


# ── R2: registered tool set matches the AST-derived expectation ───────────

def test_registered_tools_match_ast_derived_set():
    expected = _ast_derived_tool_names()
    assert len(expected) == 18, (
        f"AST-derived tool set has {len(expected)} names, expected 18: "
        f"{sorted(expected)}"
    )

    tools = _list_tools_sync()
    actual = {t.name for t in tools}

    missing = expected - actual
    extra = actual - expected
    assert actual == expected, (
        "registered tool set does not match the AST-derived expectation; "
        f"missing={sorted(missing)} extra={sorted(extra)}"
    )
    assert len(actual) == 18


# ── R3: tool metadata sampled via the real client ──────────────────────────

def test_navigate_tool_schema_via_client():
    tools = _list_tools_sync()
    by_name = {t.name: t for t in tools}
    nav = by_name["navigate"]

    assert (
        "Navigate the current Chrome page to *url* and wait for it to load."
        in nav.description
    ), nav.description

    props = nav.inputSchema.get("properties", {})
    assert set(props.keys()) == {"url", "wait_until"}, props.keys()
    assert props["url"]["type"] == "string"
    assert props["wait_until"].get("default") == "load"
    assert nav.inputSchema.get("required") == ["url"]


def test_get_page_info_has_no_required_properties():
    tools = _list_tools_sync()
    by_name = {t.name: t for t in tools}
    gpi = by_name["get_page_info"]

    required = gpi.inputSchema.get("required", [])
    assert required == [], required
    properties = gpi.inputSchema.get("properties", {})
    assert properties == {}, (
        f"get_page_info takes no arguments, so its schema should declare no "
        f"properties at all — got {properties!r}"
    )


def test_sleep_seconds_param_is_number_and_required():
    tools = _list_tools_sync()
    by_name = {t.name: t for t in tools}
    sl = by_name["sleep"]

    props = sl.inputSchema.get("properties", {})
    assert props["seconds"]["type"] == "number", props.get("seconds")
    assert "seconds" in sl.inputSchema.get("required", [])


def test_dict_returning_tool_yields_json_text_content():
    """A dict-returning tool (get_page_info) must still yield a type=="text"
    content block whose payload round-trips through json.loads — verifying
    fastmcp 2.x's structured-output additions don't replace the text block."""
    import asyncio
    import json
    from pathlib import Path as _Path
    from unittest import mock

    import fastmcp

    import chrome_wrapper_plugin.server as server_module
    from chrome_wrapper_plugin.server import ChromeEngine

    engine = ChromeEngine(
        proc=None,
        port=9222,
        user_data_dir=_Path("/tmp/udd"),
        session_id="test-session",
    )
    engine.session = mock.MagicMock()
    engine.session.send.return_value = {
        "targetInfo": {"url": "http://example.test", "title": "Example"}
    }
    expected_payload = {"url": "http://example.test", "title": "Example"}

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool("get_page_info", {})

    with mock.patch.object(server_module, "_get_engine", return_value=engine):
        result = asyncio.run(_run())

    blocks = _read_content_blocks(result)
    text_blocks = [b for b in blocks if getattr(b, "type", None) == "text"]
    assert text_blocks, f"no type=='text' content block in {blocks!r}"
    payload = json.loads(text_blocks[0].text)
    assert payload == expected_payload


# ── R4: screenshot wire format ─────────────────────────────────────────────

def test_screenshot_returns_image_content_block():
    import asyncio
    import base64
    from pathlib import Path as _Path
    from unittest import mock

    import fastmcp

    import chrome_wrapper_plugin.server as server_module
    from chrome_wrapper_plugin.server import ChromeEngine

    png_bytes = b"\x89PNG\r\n\x1a\n fake screenshot content"
    engine = ChromeEngine(
        proc=None,
        port=9222,
        user_data_dir=_Path("/tmp/udd"),
        session_id="test-session",
    )
    engine.session = mock.MagicMock()
    engine.session.send.return_value = {
        "data": base64.b64encode(png_bytes).decode()
    }

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool("screenshot", {})

    with mock.patch.object(server_module, "_get_engine", return_value=engine):
        result = asyncio.run(_run())

    blocks = _read_content_blocks(result)
    image_blocks = [b for b in blocks if getattr(b, "type", None) == "image"]
    assert image_blocks, f"no type=='image' content block in {blocks!r}"
    block = image_blocks[0]
    assert block.mimeType == "image/png"
    assert base64.b64decode(block.data) == png_bytes
