"""Guards over the `mcp` and `fastmcp` dependency declarations.

Ticket #25 capped `mcp` at `<2` as a stopgap because `mcp` 2.0.0 dropped the
`mcp.server.fastmcp` module that `src/chrome_wrapper_plugin/server.py` (and
formerly `tests/test_tools.py`) imported. Ticket #26 migrates the server
onto the standalone `fastmcp>=2,<3` package and lifts the `mcp` cap so that
`mcp` 2.0.0 (and later 2.x releases) is admitted again — `fastmcp` pins its
own compatible `mcp` range independently of our declared ceiling.

This file must not import `chrome_wrapper_plugin` or `fastmcp` at module
scope: importing either inside a test body (rather than at module scope)
keeps a broken/mid-migration environment legible as a named failing test
instead of a bare collection error that takes the whole file down.
"""
import importlib.metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
THIS_FILE = Path(__file__).resolve()


def _load_dependencies():
    with open(PYPROJECT_PATH, "rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["dependencies"]


def _find_requirements(name: str):
    return [
        req
        for req in (Requirement(entry) for entry in _load_dependencies())
        if req.name == name
    ]


def test_mcp_dependency_admits_2x_excludes_3x():
    """The declared `mcp` constraint must admit 2.0.0 but exclude 3.0.0.

    #26 migrates the server off the vendored FastMCP inside `mcp<2` and onto
    the standalone `fastmcp` package, so the #25 stopgap cap that excluded
    2.0.0 must now be raised — while still capping below a hypothetical
    unreviewed 3.0.0.
    """
    mcp_reqs = _find_requirements("mcp")
    assert mcp_reqs, "no 'mcp' entry found in [project.dependencies]"
    specifier = mcp_reqs[0].specifier
    assert specifier.contains("1.27.1"), (
        f"mcp specifier {str(specifier)!r} excludes 1.27.1; the whole point "
        "of keeping `mcp` declared per #26's Q3 is to still admit resolving "
        "to a 1.x release"
    )
    assert specifier.contains("2.0.0"), (
        f"mcp specifier {str(specifier)!r} still excludes 2.0.0; #26 must "
        "raise the #25 stopgap cap now that the server no longer needs "
        "mcp.server.fastmcp"
    )
    assert not specifier.contains("3.0.0"), (
        f"mcp specifier {str(specifier)!r} admits 3.0.0; keep an upper "
        "bound below the next major"
    )


def test_mcp_is_declared_exactly_once():
    """Sanity check: only one `mcp` entry in [project.dependencies]."""
    assert len(_find_requirements("mcp")) == 1


def test_fastmcp_dependency_declared():
    """A `fastmcp` requirement must exist, admitting 2.x but not 3.x."""
    fastmcp_reqs = _find_requirements("fastmcp")
    assert fastmcp_reqs, "no 'fastmcp' entry found in [project.dependencies]"
    specifier = fastmcp_reqs[0].specifier
    assert specifier.contains("2.0.0"), (
        f"fastmcp specifier {str(specifier)!r} must admit 2.0.0"
    )
    assert not specifier.contains("1.9.9"), (
        f"fastmcp specifier {str(specifier)!r} has no lower bound at 2.x — "
        "a bare `fastmcp<3` with no floor would wrongly pass this test"
    )
    assert not specifier.contains("3.0.0"), (
        f"fastmcp specifier {str(specifier)!r} must not admit 3.0.0"
    )


def test_fastmcp_is_declared_exactly_once():
    """Sanity check: only one `fastmcp` entry in [project.dependencies]."""
    assert len(_find_requirements("fastmcp")) == 1


def test_no_vendored_fastmcp_import_remains():
    """No src/ or tests/ file may still reference mcp.server.fastmcp.

    #26 replaces every `from mcp.server.fastmcp import ...` with the
    standalone `fastmcp` package. This file itself is excluded since its
    own docstring/body legitimately mentions the string.
    """
    hits = []
    for pattern in ("src/**/*.py", "tests/**/*.py"):
        for path in REPO_ROOT.glob(pattern):
            if path.resolve() == THIS_FILE:
                continue
            text = path.read_text(encoding="utf-8")
            if "mcp.server.fastmcp" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, (
        f"found leftover 'mcp.server.fastmcp' references in: {hits}; "
        "migrate these to the standalone fastmcp package (#26)"
    )


def test_fastmcp_is_importable_and_major_version_2():
    """The installed `fastmcp` package must be importable and major version 2.

    Replaces the retired `test_installed_mcp_provides_fastmcp`: we no longer
    assert on the installed `mcp` major, since `fastmcp` pins its own `mcp`
    range independently of chrome_wrapper_plugin's declared ceiling.
    """
    import fastmcp  # noqa: F401  (import inside test body, see module docstring)

    installed_version = Version(importlib.metadata.version("fastmcp"))
    assert installed_version.major == 2, (
        f"installed fastmcp version {installed_version} is not major 2.x"
    )
