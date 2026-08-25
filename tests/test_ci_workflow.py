"""Guards over the `tests` GitHub Actions workflow's trigger, and over the
release/dispatch workflows staying dispatch-only and test/lint-free (#33).

Ticket #33: `.github/workflows/test.yml` currently triggers on both `push`
(branches: ["**"]) and `pull_request`, so a branch with an open PR gets two
duplicate `tests` runs for the same commit. These tests assert invariants
over the workflow YAML files themselves (not a GitHub Actions replay),
matching the existing style in tests/test_release_workflow.py.
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"
DISPATCH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "dispatch.yml"

# Case-insensitive, word-bounded test/lint tool invocation inside a `run:` script.
_RUN_TOOL_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|coverage|ruff|flake8|pylint|mypy|black|isort|bandit)\b",
    re.IGNORECASE,
)
# `uses:` denylist -- substring match. Safe against the current `uses:` set
# (actions/checkout@v4, actions/upload-artifact@v4, actions/download-artifact@v4,
# actions/setup-python@v5), none of which contains "test" or "lint".
_USES_TOOL_RE = re.compile(
    r"pytest|super-linter|lint|ruff|flake8|mypy|psf/black|test",
    re.IGNORECASE,
)

# Trigger-claim phrases the header comment must not contain once push is dropped.
_PUSH_TRIGGER_PHRASES = (
    "on every push",
    "on each push",
    "runs on push",
    "on push",
    "push to any branch",
    "every push to",
)


def _load_workflow(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _triggers(workflow: dict):
    """PyYAML is YAML 1.1, so the bare `on:` key parses as boolean True."""
    return workflow.get("on", workflow.get(True))


def _header_comment_text(path: Path) -> str:
    """Text of the '#' comment lines that precede the `on:` trigger block,
    lowercased. Stops at the first line starting with `on:` so it only
    covers the header, not later inline comments."""
    collected = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("on:"):
            break
        if stripped.startswith("#"):
            collected.append(stripped.lstrip("#").strip())
    return "\n".join(collected).lower()


def _tool_gate_violations(path: Path):
    workflow = _load_workflow(path)
    violations = []
    jobs = workflow.get("jobs", {})
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            run = step.get("run")
            if run:
                for lineno, line in enumerate(run.splitlines(), start=1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if _RUN_TOOL_RE.search(stripped):
                        violations.append(
                            f"{path.name} job={job_name!r} "
                            f"step={step.get('name')!r} run-line {lineno}: {line!r}"
                        )
            uses = step.get("uses")
            if uses and _USES_TOOL_RE.search(uses):
                violations.append(
                    f"{path.name} job={job_name!r} "
                    f"step={step.get('name')!r} uses={uses!r}"
                )
    return violations


def test_test_workflow_triggers_on_pull_request_only():
    """#33: a branch with an open PR must not also get a `push` run."""
    workflow = _load_workflow(TEST_WORKFLOW_PATH)
    triggers = _triggers(workflow)
    assert set(triggers.keys()) == {"pull_request"}, (
        f"expected `tests` workflow trigger key set {{'pull_request'}}, got "
        f"{set(triggers.keys())!r} -- a branch with an open PR gets a "
        "duplicate run for every push (#33)"
    )


def test_test_workflow_has_no_push_branch_filter_anywhere():
    raw = TEST_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'branches: ["**"]' not in raw, (
        'found a `branches: ["**"]` push filter still present in '
        f"{TEST_WORKFLOW_PATH} -- #33 removes the push trigger entirely, "
        "not just narrows it"
    )


def test_test_workflow_header_comment_does_not_describe_a_push_trigger():
    """Phrase-level check (not a bare token ban on 'push'): the header may
    still explain, in prose, why the push trigger was removed (#33) -- it
    just must not claim the workflow still runs on push."""
    comment = _header_comment_text(TEST_WORKFLOW_PATH)
    for phrase in _PUSH_TRIGGER_PHRASES:
        assert phrase not in comment, (
            f"header comment of {TEST_WORKFLOW_PATH} still contains the "
            f"trigger-claim phrase {phrase!r} -- rewrite the header to stop "
            "describing a push trigger (#33)"
        )
    assert "pull request" in comment, (
        f"header comment of {TEST_WORKFLOW_PATH} should describe the "
        "pull-request-only trigger"
    )


def test_release_and_dispatch_workflows_stay_dispatch_only_and_ungated():
    """Out-of-scope guard (#33): release.yml and dispatch.yml must keep
    workflow_dispatch as their only trigger, and neither may grow a test/lint
    gate of its own -- a `workflow_dispatch`-triggered pytest step would sail
    past a trigger-only assertion, so this also scans run/uses for gates."""
    for path in (RELEASE_WORKFLOW_PATH, DISPATCH_WORKFLOW_PATH):
        workflow = _load_workflow(path)
        triggers = _triggers(workflow)
        assert set(triggers.keys()) == {"workflow_dispatch"}, (
            f"{path.name} trigger key set must be exactly "
            f"{{'workflow_dispatch'}}, got {set(triggers.keys())!r}"
        )
        violations = _tool_gate_violations(path)
        assert not violations, (
            f"{path.name} contains what looks like a test/lint gate step, "
            f"which must stay reserved for the `tests` workflow: {violations!r}"
        )


def test_test_workflow_job_shape_unchanged():
    """Regression guard: #33 only touches the trigger + header comment, not
    the job's matrix/timeout/step shape."""
    workflow = _load_workflow(TEST_WORKFLOW_PATH)
    job = workflow["jobs"]["pytest"]
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["strategy"]["matrix"]["os"] == ["windows-latest"]
    assert job["timeout-minutes"] == 10
    step_names = [step.get("name") for step in job["steps"]]
    assert "Run pytest" in step_names
