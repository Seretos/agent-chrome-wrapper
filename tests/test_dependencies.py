"""Guards over the `mcp` dependency declaration.

Ticket #25: CI has been red on every run since `mcp` 2.0.0 was published.
`pyproject.toml` declared `mcp>=1.2` with no upper bound, so a fresh CI
install resolves `mcp` 2.0.0, which dropped the `mcp.server.fastmcp`
module that `src/chrome_wrapper_plugin/server.py` and `tests/test_tools.py`
both import. Migrating to the mcp 2.x API is explicitly out of scope for
this ticket — tracked as #26.

This file must not import anything from `chrome_wrapper_plugin`: under an
uncapped/mcp-2.x install, `tests/test_smoke.py` fails at *collection* time
with a bare `ModuleNotFoundError` because it imports
`chrome_wrapper_plugin.server` at module scope. Importing `mcp.server.fastmcp`
inside a test body here (rather than at module scope) keeps that failure
mode legible as a named failing test instead of a collection error.
"""
import importlib.metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_dependencies():
    with open(PYPROJECT_PATH, "rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["dependencies"]


def _find_mcp_requirements():
    return [
        req
        for req in (Requirement(entry) for entry in _load_dependencies())
        if req.name == "mcp"
    ]


def test_mcp_dependency_excludes_2x():
    """The declared `mcp` constraint in pyproject.toml must exclude 2.x.

    `mcp` 2.0.0 dropped `mcp.server.fastmcp`, which both
    `src/chrome_wrapper_plugin/server.py` and `tests/test_tools.py` import.
    Migrating to the mcp 2.x API is tracked separately as #26; until then,
    the declared specifier must not admit 2.0.0.
    """
    mcp_reqs = _find_mcp_requirements()
    assert mcp_reqs, "no 'mcp' entry found in [project.dependencies]"
    specifier = mcp_reqs[0].specifier
    assert not specifier.contains("2.0.0"), (
        f"mcp specifier {str(specifier)!r} admits 2.0.0, which dropped "
        "mcp.server.fastmcp (see #25); add an upper bound (e.g. '<2') "
        "until the mcp 2.x migration (#26) lands"
    )
    assert specifier.contains("1.27.1"), (
        f"mcp specifier {str(specifier)!r} unexpectedly excludes the known-"
        "working 1.27.1 release"
    )


def test_mcp_is_declared_exactly_once():
    """Sanity check: only one `mcp` entry in [project.dependencies]."""
    assert len(_find_mcp_requirements()) == 1


def test_installed_mcp_provides_fastmcp():
    """Coverage, not the driving test — see module docstring for why.

    This assertion cannot catch the #25 regression by itself: it only goes
    RED in a freshly-resolved environment installed from the *uncapped*
    pyproject.toml (the CI path). A developer's local venv that already
    holds a stale pre-2.0 `mcp` install (as this repo's did) will pass this
    test both before and after the fix, because the installed package never
    changes underneath it. Its value is diagnostic and drift-catching: it
    names the actual missing module instead of only expressing the
    constraint in the abstract, and it is redundant with (not a substitute
    for) the three collection errors CI already emits without the cap.
    """
    import mcp.server.fastmcp  # noqa: F401  (import inside test body, see module docstring)

    installed_version = Version(importlib.metadata.version("mcp"))
    assert installed_version.major < 2, (
        f"installed mcp version {installed_version} is 2.x; "
        "mcp.server.fastmcp import above should already have failed for "
        "this environment (see #26 for the mcp 2.x migration)"
    )
