"""Packaging-level tests over the release pipeline definition.

Ticket #23: `bin/chrome-wrapper.exe` lands on the orphan `release` branch as
mode 100644 (no Unix execute bit), so WSL's interop layer refuses to
`posix_spawn` it after `/plugin install`. These tests assert invariants over
`.github/workflows/release.yml` itself (not a git-behaviour simulation,
since CI runs `windows-latest` where `core.fileMode` defaults to `false` and
would make a replay pass on the buggy code too).
"""
import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"

STAGE_STEP_NAME = "Build merged staging tree"
RELEASE_BRANCH_STEP_NAME = "Push orphan release branch and capture commit"
EXE_PATH = "bin/chrome-wrapper.exe"


def _load_workflow():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _find_step(job_name, step_name):
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    assert job_name in jobs, f"job {job_name!r} not found in {WORKFLOW_PATH}"
    steps = jobs[job_name]["steps"]
    for step in steps:
        if step.get("name") == step_name:
            return step
    raise AssertionError(
        f"step {step_name!r} not found in job {job_name!r} of {WORKFLOW_PATH}"
    )


def _step_run_lines(job_name, step_name):
    step = _find_step(job_name, step_name)
    run = step.get("run", "")
    return run.splitlines()


def test_staging_step_chmods_the_exe():
    """The staging step must set the Unix execute bit on the real .exe file,
    before any `cp -a`/`git add` downstream can act on it — mode-only fixes
    (e.g. `git update-index --chmod`) don't survive a later `git add -A`."""
    lines = _step_run_lines("assemble", STAGE_STEP_NAME)
    chmod_lines = [
        line
        for line in lines
        if "chmod +x" in line and f"$STAGE/{EXE_PATH}" in line
    ]
    assert chmod_lines, (
        f"expected a `chmod +x .../{EXE_PATH}` line in the "
        f"{STAGE_STEP_NAME!r} step's run script, found none"
    )


def test_no_git_add_after_chmod_in_release_branch_step():
    """Reproduces the reported defect: `git add -A` re-stats the working-tree
    file and silently reverts a preceding `git update-index --chmod=+x`.
    No `git add` may run after the chmod call."""
    lines = _step_run_lines("assemble", RELEASE_BRANCH_STEP_NAME)

    chmod_indices = [
        i for i, line in enumerate(lines) if "git update-index --chmod=+x" in line
    ]
    assert chmod_indices, (
        f"expected a `git update-index --chmod=+x` line in the "
        f"{RELEASE_BRANCH_STEP_NAME!r} step's run script, found none"
    )

    add_indices = [i for i, line in enumerate(lines) if "git add" in line]
    assert add_indices, (
        f"expected at least one `git add` line in the "
        f"{RELEASE_BRANCH_STEP_NAME!r} step's run script, found none"
    )

    last_chmod = max(chmod_indices)
    later_adds = [i for i in add_indices if i > last_chmod]
    assert not later_adds, (
        "found `git add` line(s) after `git update-index --chmod=+x` "
        f"(lines {later_adds!r} of the step script) — `git add -A` re-stats "
        "the working-tree file and reverts the chmod before commit"
    )


def test_release_branch_step_asserts_committed_mode():
    """The orphan-branch step must self-verify the staged mode is 100755 and
    fail the release loudly otherwise, so a future refactor can't silently
    regress the bit."""
    lines = _step_run_lines("assemble", RELEASE_BRANCH_STEP_NAME)
    run = "\n".join(lines)

    assert "git ls-files -s" in run and EXE_PATH in run, (
        f"expected a `git ls-files -s {EXE_PATH}` mode check in the "
        f"{RELEASE_BRANCH_STEP_NAME!r} step"
    )
    assert "100755" in run, (
        f"expected the {RELEASE_BRANCH_STEP_NAME!r} step to assert against "
        "mode 100755"
    )
    assert "exit 1" in run, (
        f"expected the {RELEASE_BRANCH_STEP_NAME!r} step's mode check to "
        "fail the job (exit 1) when the assertion doesn't hold"
    )


def test_zip_block_stamps_exec_attr_on_exe():
    """Regression guard: the GitHub Release zip asset must keep stamping the
    Unix exec bit into its central directory for the .exe — this is the
    artifact that was already correct and must not regress."""
    lines = _step_run_lines("assemble", STAGE_STEP_NAME)
    run = "\n".join(lines)

    assert "EXE_ATTR = (0o755 << 16) | 0x8000" in run, (
        "expected the zip-building python3 block to stamp "
        "EXE_ATTR = (0o755 << 16) | 0x8000"
    )
    assert f'"{EXE_PATH}"' in run, (
        f"expected {EXE_PATH!r} to be listed among the zip's EXECS"
    )


def test_exe_path_is_consistent_across_manifests_and_workflow():
    """The path the workflow chmods/asserts must match the `command` both
    plugin manifests actually launch — guards against the chmod silently
    targeting a stale filename after a rename."""
    stage_run = "\n".join(_step_run_lines("assemble", STAGE_STEP_NAME))
    release_run = "\n".join(_step_run_lines("assemble", RELEASE_BRANCH_STEP_NAME))

    assert f"$STAGE/{EXE_PATH}" in stage_run
    assert EXE_PATH in release_run

    for manifest_name in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        manifest_path = REPO_ROOT / manifest_name
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        command = manifest["mcpServers"]["chrome-wrapper"]["command"]
        assert command.endswith(f"/{EXE_PATH}"), (
            f"{manifest_name} command {command!r} does not reference "
            f"{EXE_PATH!r} — the workflow's chmod/assert would target a "
            "stale filename"
        )
