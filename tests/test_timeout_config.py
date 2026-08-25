"""Guards over the per-test timeout configuration (#30).

`pytest-timeout` bounds a hung test (heavy subprocess/network blocking
surface in this suite) to a fixed wall-clock budget instead of wedging CI for
the full `timeout-minutes: 10` job budget in .github/workflows/test.yml. The
thread method (not signal) is required because SIGALRM is unavailable on
Windows, and its stack dump is the diagnostic payload we want when a test
does hang.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
TEST_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
INTEGRATION_TEST_PATH = REPO_ROOT / "tests" / "test_integration.py"


def _load_pyproject():
    with open(PYPROJECT_PATH, "rb") as fh:
        return tomllib.load(fh)


def test_timeout_ini_options_are_active(pytestconfig):
    """Driving test for Requirement B: the resolved ini values (not the TOML
    text) must reflect a 60s, thread-method timeout."""
    assert float(pytestconfig.getini("timeout")) == 60.0
    assert pytestconfig.getini("timeout_method") == "thread"


def test_pytest_timeout_is_a_declared_test_dependency():
    data = _load_pyproject()
    test_extra = data["project"]["optional-dependencies"]["test"]
    reqs = [Requirement(entry) for entry in test_extra]
    matches = [req for req in reqs if req.name == "pytest-timeout"]
    assert matches, (
        "no 'pytest-timeout' entry found in "
        "[project.optional-dependencies].test of pyproject.toml"
    )
    specifier = matches[0].specifier
    assert str(specifier) != "", (
        "pytest-timeout entry has no version specifier at all -- an "
        "unconstrained 'pytest-timeout' entry would silently admit any "
        "version, including ones predating the thread-timeout support "
        "this ticket relies on"
    )
    # Checking `specifier.contains("2.3")` only asks whether 2.3 happens to
    # be admitted -- a later, still-compliant tightening to e.g. ">=2.4"
    # would exclude the literal "2.3" and fail this test even though it is
    # a *stricter* lower bound than the plan requires. What actually matters
    # is that a lower-bound clause exists at all.
    lower_bound_ops = {">=", "==", "~=", ">"}
    assert any(spec.operator in lower_bound_ops for spec in specifier), (
        f"pytest-timeout dependency has no lower-bound version constraint: "
        f"{specifier!r}"
    )


def test_ci_install_step_installs_the_test_extra():
    """FIX-2, verified-no-change-needed guard: the CI install step must keep
    installing the `[test]` extra, so a future 'slim down CI' edit that drops
    it fails loudly instead of silently disabling the timeout."""
    import yaml

    with open(TEST_WORKFLOW_PATH, "r", encoding="utf-8") as fh:
        workflow = yaml.safe_load(fh)
    steps = workflow["jobs"]["pytest"]["steps"]
    install_step = next(
        (s for s in steps if s.get("name") == "Install plugin + test deps"), None
    )
    assert install_step is not None, (
        "expected a step named 'Install plugin + test deps' in the "
        "`pytest` job of .github/workflows/test.yml"
    )
    assert "[test]" in install_step.get("run", ""), (
        "'Install plugin + test deps' step no longer installs the [test] "
        "extra -- this is what makes pytest-timeout (and its ini config) "
        "take effect on the CI runner"
    )
    step_names = [s.get("name") for s in steps]
    assert "Run pytest" in step_names


def test_pytest_timeout_is_importable():
    import pytest_timeout  # noqa: F401


def test_timeout_method_is_thread_not_signal(pytestconfig):
    """SIGALRM is unavailable on Windows, and the thread method's stack dump
    is the diagnostic payload we want -- do not 'fix' this to signal."""
    assert pytestconfig.getini("timeout_method") == "thread"


def test_long_running_integration_test_raises_rather_than_removes_the_timeout():
    """tests/test_integration.py::test_navigate_and_screenshot legitimately
    takes close to 100s worst case (three 30s-deadline _send_recv calls plus
    an 8s proc.wait) -- it must get an individually raised marker, not a
    lowered global timeout."""
    text = INTEGRATION_TEST_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        target_idx = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("def test_navigate_and_screenshot(")
        )
    except StopIteration:
        raise AssertionError(
            "test_navigate_and_screenshot not found in "
            f"{INTEGRATION_TEST_PATH}"
        )

    marker_match = None
    for line in reversed(lines[:target_idx]):
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"@pytest\.mark\.timeout\((\d+)\)", stripped)
        if match:
            marker_match = match
            break
        if stripped.startswith("@"):
            continue
        # Any other non-decorator line means we've walked past the
        # decorator block without finding the marker.
        break

    assert marker_match is not None, (
        "expected an @pytest.mark.timeout(N) decorator directly above "
        "test_navigate_and_screenshot"
    )
    assert int(marker_match.group(1)) >= 120, (
        f"test_navigate_and_screenshot's @pytest.mark.timeout value "
        f"{marker_match.group(1)} is below the 120s floor needed to cover "
        "its ~100s worst case"
    )


def test_a_blocking_test_is_killed_by_the_timeout():
    """Driving test for Requirement C: a genuinely hanging test must
    actually be aborted by pytest-timeout, not just accept the CLI flags.

    Runs in an isolated tmp_path so the repo's own ini config can't leak in
    (cwd=tmp_path + a bare filename keeps rootdir there), and strips
    PYTEST_ADDOPTS so no ambient env var smuggles in extra behaviour.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        (tmp_path / "test_hang.py").write_text(
            "import time\n\n\ndef test_hang():\n    time.sleep(3600)\n"
        )
        env = os.environ.copy()
        env.pop("PYTEST_ADDOPTS", None)

        start = time.monotonic()
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                "-p", "no:cacheprovider",
                "--timeout=2", "--timeout-method=thread",
                "test_hang.py",
            ],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        elapsed = time.monotonic() - start

    combined = result.stdout + result.stderr

    assert "unrecognized arguments" not in combined, (
        f"pytest rejected --timeout/--timeout-method as unknown flags -- "
        f"pytest-timeout is not active in this interpreter's environment. "
        f"Output tail:\n{combined[-2000:]}"
    )
    assert result.returncode != 4, (
        f"pytest exited with USAGE_ERROR (4), meaning --timeout/"
        f"--timeout-method were rejected. Output tail:\n{combined[-2000:]}"
    )
    assert re.search(r"\+{3,}\s*Timeout\s*\+{3,}", combined), (
        f"expected pytest-timeout's own '+++ Timeout +++' banner in the "
        f"output. Output tail:\n{combined[-2000:]}"
    )
    assert "Stack of " in combined, (
        f"expected the thread-method stack-dump header ('Stack of ...') in "
        f"the output. Output tail:\n{combined[-2000:]}"
    )
    assert elapsed < 60, (
        f"blocking test took {elapsed:.1f}s to be killed -- the 2s timeout "
        "did not actually abort it in a reasonable time"
    )
