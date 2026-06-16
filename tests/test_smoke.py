"""Smoke tests: verify that every expected tool function is importable and callable."""
import chrome_wrapper_plugin.server as server_module


EXPECTED_TOOLS = [
    "navigate",
    "get_page_info",
    "screenshot",
    "evaluate_js",
    "cdp",
    "get_instance_info",
]


def test_expected_tools_exist_and_are_callable():
    """All MVP tool functions must exist on the server module and be callable."""
    for name in EXPECTED_TOOLS:
        fn = getattr(server_module, name, None)
        assert fn is not None, f"Tool {name!r} not found in server module"
        assert callable(fn), f"Tool {name!r} is not callable"


def test_ping_removed():
    """ping() must no longer exist — it was a placeholder."""
    assert not hasattr(server_module, "ping"), (
        "ping() still exists in server module but should have been removed"
    )
