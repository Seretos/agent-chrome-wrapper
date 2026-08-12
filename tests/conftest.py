"""Shared pytest fixtures for the chrome_wrapper_plugin test suite."""

from __future__ import annotations

import pytest

# All env-vars resolve_session_id() probes, in probe order.
_SESSION_ENV_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "ANTHROPIC_SESSION_ID",
    "MCP_SESSION_ID",
)


@pytest.fixture(autouse=True)
def _isolate_session_env(monkeypatch):
    """Clear every session-id env-var before each test.

    Without this, the suite's outcome would depend on the ambient
    environment it happens to run under (e.g. a real Claude Code session
    exporting CLAUDE_CODE_SESSION_ID) rather than on the code under test.
    """
    for var in _SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
