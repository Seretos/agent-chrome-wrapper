"""Tests for ticket #34: a `changelog` field in the `plugin-release`
`repository_dispatch` payload this repo POSTs to `Seretos/agent-marketplace`.

Two complementary layers, mirroring `tests/test_release_workflow.py`'s
established YAML-parsing pattern (`_load_workflow`/`_find_step`), extended
with a `path` parameter so both workflow files can share the same helpers:

- *content layer (exec)*: extract the `python3 - <<'PY' ... PY` heredoc that
  builds the request body out of the "Dispatch to agent-marketplace" step's
  `run:` text, `exec()` it in-process with env vars set via `monkeypatch`,
  then `json.load` the file it wrote and assert on structure/values.
  `capsys` captures the `::warning::` line.
- *wiring layer (text)*: assert directly against the step's raw `run:`
  string and/or its `env:` mapping, parsed straight out of the YAML -- for
  shell plumbing exec can't observe (the separate fetch step, curl
  flags/URL, heredoc quoting, what `env:` keys are bound to).

Every behavioural test that has a counterpart in both workflows is
parametrized over `workflow` in `{release.yml, dispatch.yml}`.
"""
import json
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
DISPATCH_YML = REPO_ROOT / ".github" / "workflows" / "dispatch.yml"

BOTH_WORKFLOWS = [RELEASE_YML, DISPATCH_YML]
WORKFLOW_IDS = ["release.yml", "dispatch.yml"]

JOB_NAME = {
    RELEASE_YML: "assemble",
    DISPATCH_YML: "dispatch",
}

DISPATCH_STEP_NAME = "Dispatch to agent-marketplace"
FETCH_STEP_NAME = "Fetch changelog for dispatch"
RELEASE_CREATE_STEP_NAME = "Create tag and GitHub Release"

# Same TAG-source expression the dispatch step's own TAG env is bound to in
# each workflow -- the fetch step's TAG must match it so `ref` and the
# changelog can never disagree (R1).
TAG_EXPRESSION = {
    RELEASE_YML: "needs.stamp.outputs.tag",
    DISPATCH_YML: "steps.tag.outputs.tag",
}

CURL_URL = "https://api.github.com/repos/Seretos/agent-marketplace/dispatches"
CURL_FLAGS = "curl -fsSL -X POST"
AUTH_HEADER = '-H "Authorization: Bearer $GH_PAT"'
ACCEPT_HEADER = '-H "Accept: application/vnd.github+json"'

FROZEN_ENV_KEYS = {
    "GH_PAT", "NAME", "DESC", "VERSION", "TAG", "REPO",
    "CHANGELOG_FILE", "PAYLOAD_FILE",
}

DEFAULT_ENV = {
    "NAME": "agent-chrome-wrapper",
    "DESC": "A Chrome wrapper MCP server.",
    "VERSION": "0.0.1",
    "TAG": "agent-chrome-wrapper--v0.0.1",
    "REPO": "Seretos/agent-chrome-wrapper",
}

TRUNCATE_LIMIT = 30000

HOSTILE_CHANGELOG = (
    "Backticks `like this`, \"double quotes\", and \\backslashes\\.\n"
    "$(rm -rf /)\n"
    "${{ github.token }}\n"
    "EOF\n"
    "trailing line\r\n"
)


# ---------------------------------------------------------------------------
# Helpers -- mirrors tests/test_release_workflow.py's _load_workflow /
# _find_step, extended with a `path` parameter so both workflow files can
# share the same helpers.
# ---------------------------------------------------------------------------

def _load_workflow(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _find_step(path, job_name, step_name):
    workflow = _load_workflow(path)
    jobs = workflow["jobs"]
    assert job_name in jobs, f"job {job_name!r} not found in {path}"
    steps = jobs[job_name]["steps"]
    for step in steps:
        if step.get("name") == step_name:
            return step
    raise AssertionError(
        f"step {step_name!r} not found in job {job_name!r} of {path}"
    )


def _dispatch_step(path):
    return _find_step(path, JOB_NAME[path], DISPATCH_STEP_NAME)


def _fetch_step(path):
    return _find_step(path, JOB_NAME[path], FETCH_STEP_NAME)


def _step_index(path, job_name, step_name):
    workflow = _load_workflow(path)
    jobs = workflow["jobs"]
    assert job_name in jobs, f"job {job_name!r} not found in {path}"
    steps = jobs[job_name]["steps"]
    for index, step in enumerate(steps):
        if step.get("name") == step_name:
            return index
    raise AssertionError(
        f"step {step_name!r} not found in job {job_name!r} of {path}"
    )


def _extract_heredoc_block(run_text, open_marker="<<'PY'", end_token="PY"):
    """Extract the body of a quoted heredoc (default delimiter PY) out of a
    step's parsed `run:` text. Returns None if no such heredoc is present.

    By the time YAML has parsed a `run: |` block, the block's own common
    indentation has already been stripped, so a `PY` terminator line that
    merely mirrors the surrounding heredoc-body indentation in the raw file
    arrives here as a bare `PY` -- this is what makes the heredoc valid bash
    *and* lets this helper find it with a plain per-line scan.
    """
    lines = run_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if open_marker in line:
            start = i + 1
            break
    if start is None:
        return None
    end = None
    for i in range(start, len(lines)):
        if lines[i].strip() == end_token:
            end = i
            break
    if end is None:
        return None
    return textwrap.dedent("\n".join(lines[start:end]))


def _extract_payload_builder(path):
    step = _dispatch_step(path)
    run_text = step.get("run", "")
    return _extract_heredoc_block(run_text)


def _expected_client_payload(env):
    return {
        "name": env["NAME"],
        "description": env["DESC"],
        "repo": env["REPO"],
        "category": "mcp",
        "version": env["VERSION"],
        "ref": env["TAG"],
        "icon": f"https://raw.githubusercontent.com/{env['REPO']}/{env['TAG']}/assets/icon.png",
        "description_url": f"https://raw.githubusercontent.com/{env['REPO']}/{env['TAG']}/description.md",
    }


FROZEN_CLIENT_PAYLOAD_KEYS = set(_expected_client_payload(DEFAULT_ENV))


def _run_builder(
    path,
    monkeypatch,
    tmp_path,
    *,
    env_overrides=None,
    changelog_present=True,
    changelog_body="",
):
    """Set env vars, exec the extracted payload-builder heredoc, and return
    (payload_file_path, env_used). Raises a plain, legible AssertionError
    (not an import/syntax crash) when no heredoc exists yet -- that is the
    expected RED reason pre-change for almost every exec-layer test here."""
    env = dict(DEFAULT_ENV)
    if env_overrides:
        env.update(env_overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    payload_file = tmp_path / "payload.json"
    changelog_file = tmp_path / "changelog.md"
    if changelog_present:
        changelog_file.write_text(changelog_body, encoding="utf-8", newline="")
    elif changelog_file.exists():
        changelog_file.unlink()

    monkeypatch.setenv("PAYLOAD_FILE", str(payload_file))
    monkeypatch.setenv("CHANGELOG_FILE", str(changelog_file))

    code = _extract_payload_builder(path)
    assert code is not None, (
        f"no `python3 - <<'PY' ... PY` heredoc found in the "
        f"{DISPATCH_STEP_NAME!r} step of {path} -- payload builder does not "
        "exist yet"
    )
    exec(compile(code, str(path), "exec"), {"__name__": "__main__"})
    return payload_file, env


# ---------------------------------------------------------------------------
# R-1 -- the dispatch envelope survives the rewrite
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_body_has_plugin_release_envelope(workflow, monkeypatch, tmp_path):
    payload_file, _ = _run_builder(workflow, monkeypatch, tmp_path, changelog_present=False)
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert set(body) == {"event_type", "client_payload"}, (
        f"expected top-level keys {{'event_type', 'client_payload'}}, got {set(body)!r}"
    )
    assert body["event_type"] == "plugin-release"


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_event_type_literal_appears_in_dispatch_step_text(workflow):
    run_text = _dispatch_step(workflow).get("run", "")
    assert '"plugin-release"' in run_text


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_event_type_is_not_env_sourced(workflow):
    env_map = _dispatch_step(workflow).get("env", {})
    assert "EVENT_TYPE" not in env_map, (
        "event_type must be a literal in the payload builder, not sourced "
        "from an env var"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_envelope_has_no_extra_top_level_keys(workflow, monkeypatch, tmp_path):
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body="Some changes",
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert set(body) == {"event_type", "client_payload"}


# ---------------------------------------------------------------------------
# R0 -- pre-existing fields keep their values; REPO is bound correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_migrated_fields_match_pre_change_values(workflow, monkeypatch, tmp_path):
    payload_file, env = _run_builder(workflow, monkeypatch, tmp_path, changelog_present=False)
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    client_payload = body["client_payload"]
    expected = _expected_client_payload(env)
    assert set(client_payload) == FROZEN_CLIENT_PAYLOAD_KEYS, (
        f"expected exactly the frozen 8 keys {sorted(FROZEN_CLIENT_PAYLOAD_KEYS)}, "
        f"got {sorted(client_payload)}"
    )
    for key, value in expected.items():
        assert client_payload[key] == value, (
            f"client_payload[{key!r}] = {client_payload[key]!r}, expected {value!r}"
        )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_repo_env_is_bound_to_github_repository(workflow):
    env_map = _dispatch_step(workflow).get("env", {})
    assert "REPO" in env_map, (
        f"expected a REPO key in the {DISPATCH_STEP_NAME!r} step's env: "
        f"mapping of {workflow}, found keys {sorted(env_map)}"
    )
    assert env_map["REPO"] == "${{ github.repository }}"


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_migrated_fields_survive_hostile_scalars(workflow, monkeypatch, tmp_path):
    hostile_overrides = {
        "NAME": 'weird "name" with \\backslash\\ and `backtick`',
        "DESC": 'desc with\nnewline and "quotes"',
    }
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        env_overrides=hostile_overrides, changelog_present=False,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    client_payload = body["client_payload"]
    assert client_payload["name"] == hostile_overrides["NAME"]
    assert client_payload["description"] == hostile_overrides["DESC"]


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_repo_and_urls_come_from_env_not_actions_expressions(workflow):
    code = _extract_payload_builder(workflow)
    assert code is not None, f"no payload-builder heredoc found in {workflow} yet"
    assert "${{" not in code


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_icon_and_description_url_are_built_from_repo_env(workflow, monkeypatch, tmp_path):
    payload_file, env = _run_builder(
        workflow, monkeypatch, tmp_path,
        env_overrides={"REPO": "SomeOrg/some-other-repo"},
        changelog_present=False,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    client_payload = body["client_payload"]
    assert client_payload["icon"] == (
        f"https://raw.githubusercontent.com/SomeOrg/some-other-repo/{env['TAG']}/assets/icon.png"
    )
    assert client_payload["description_url"] == (
        f"https://raw.githubusercontent.com/SomeOrg/some-other-repo/{env['TAG']}/description.md"
    )


# ---------------------------------------------------------------------------
# R1 -- payload carries a changelog sourced from the GitHub Release body
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_payload_includes_changelog_when_release_body_present(workflow, monkeypatch, tmp_path):
    body_text = "## What's Changed\n* Added the changelog field.\n"
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body=body_text,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert body["client_payload"]["changelog"] == body_text


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_changelog_is_fetched_from_release_view(workflow):
    run_text = _fetch_step(workflow).get("run", "")
    for expected_fragment in (
        "gh release view", "--json body", "-q '.body // empty'", "2>/dev/null", "|| true",
    ):
        assert expected_fragment in run_text, (
            f"expected {expected_fragment!r} in the {FETCH_STEP_NAME!r} "
            f"step's run text of {workflow}"
        )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_changelog_is_fetched_for_the_dispatched_tag(workflow):
    fetch_step = _fetch_step(workflow)
    run_text = fetch_step.get("run", "")
    assert '"$TAG"' in run_text, (
        f'expected `gh release view` to be invoked with "$TAG" in {workflow}'
    )
    fetch_env = fetch_step.get("env", {})
    dispatch_env = _dispatch_step(workflow).get("env", {})
    expected_tag_expr = "${{ " + TAG_EXPRESSION[workflow] + " }}"
    assert fetch_env.get("TAG") == expected_tag_expr, (
        f"expected the fetch step's TAG to be {expected_tag_expr!r}, got "
        f"{fetch_env.get('TAG')!r}"
    )
    assert dispatch_env.get("TAG") == expected_tag_expr, (
        f"expected the dispatch step's TAG to be {expected_tag_expr!r}, got "
        f"{dispatch_env.get('TAG')!r}"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_fetch_step_redirects_gh_stdout_to_changelog_file(workflow):
    fetch_step = _fetch_step(workflow)
    run_text = fetch_step.get("run", "")
    fetch_env = fetch_step.get("env", {})
    changelog_var = fetch_env.get("CHANGELOG_FILE")
    assert changelog_var is not None, (
        f"expected CHANGELOG_FILE in the {FETCH_STEP_NAME!r} step's env: "
        f"mapping of {workflow}"
    )
    assert '> "$CHANGELOG_FILE"' in run_text, (
        f"expected `gh release view` output to be redirected directly to "
        f'"$CHANGELOG_FILE" in the {FETCH_STEP_NAME!r} step of {workflow}, '
        f"got run text: {run_text!r}"
    )
    # The invocation and its redirect must co-occur on the SAME line: any
    # command-substitution wrapper (single- or multi-line, e.g.
    # `BODY=$(gh release view ...)` or a multi-line `BODY=$(\n  gh release
    # view ...\n)`) necessarily separates the invocation from a direct
    # `> "$CHANGELOG_FILE"` redirect onto different lines (or removes the
    # direct redirect from the invocation's line entirely), so requiring
    # same-line co-occurrence is what actually pins down "no round-trip
    # through a variable" -- checking `gh release view` lines and the
    # `> "$CHANGELOG_FILE"` redirect's presence in the step independently
    # (as before) is satisfied by a multi-line command substitution whose
    # redirect lives elsewhere in the step.
    direct_redirect_lines = [
        line
        for line in run_text.splitlines()
        if "gh release view" in line and '> "$CHANGELOG_FILE"' in line
    ]
    assert direct_redirect_lines, (
        f"expected a `gh release view` invocation and its "
        f'`> "$CHANGELOG_FILE"` redirect on the SAME line in the '
        f"{FETCH_STEP_NAME!r} step of {workflow} -- a command substitution "
        "(single- or multi-line) that round-trips gh's stdout through a "
        "variable before redirecting it elsewhere would put the invocation "
        f"and the redirect on different lines, got run text: {run_text!r}"
    )
    for line in direct_redirect_lines:
        assert "$(" not in line, (
            "the gh release view invocation must write gh's stdout straight "
            "to $CHANGELOG_FILE via redirection, not round-trip it through a "
            f"command substitution in {workflow}, got line: {line!r}"
        )
        assert "`" not in line, (
            "the gh release view invocation must write gh's stdout straight "
            "to $CHANGELOG_FILE via redirection, not round-trip it through a "
            f"backtick command substitution in {workflow}, got line: {line!r}"
        )
    dispatch_env = _dispatch_step(workflow).get("env", {})
    assert dispatch_env.get("CHANGELOG_FILE") == changelog_var, (
        "expected the fetch step's redirection target to be the same "
        "CHANGELOG_FILE env var the dispatch step consumes"
    )


# ---------------------------------------------------------------------------
# R2 -- the payload is JSON-escaped and injection-proof
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_payload_survives_hostile_changelog(workflow, monkeypatch, tmp_path):
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body=HOSTILE_CHANGELOG,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert body["client_payload"]["changelog"] == HOSTILE_CHANGELOG


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_uses_no_unquoted_heredoc(workflow):
    run_text = _dispatch_step(workflow).get("run", "")
    assert "<<'PY'" in run_text, (
        f"expected a quoted `<<'PY'` heredoc delimiter in {DISPATCH_STEP_NAME!r} "
        f"of {workflow}"
    )
    assert "<<EOF" not in run_text, (
        "found an unquoted `<<EOF` heredoc -- this is the live shell/JSON "
        "injection hazard being fixed"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_payload_builder_has_no_actions_expressions(workflow):
    code = _extract_payload_builder(workflow)
    assert code is not None, f"no payload-builder heredoc found in {workflow} yet"
    assert "${{" not in code


# ---------------------------------------------------------------------------
# R3 -- empty/missing release body omits the key and never fails the job
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
@pytest.mark.parametrize(
    "changelog_present, changelog_body",
    [
        pytest.param(False, "", id="file-absent"),
        pytest.param(True, "   \n\t  \n", id="whitespace-only"),
    ],
)
def test_changelog_key_omitted_when_body_empty(
    workflow, changelog_present, changelog_body, monkeypatch, tmp_path
):
    payload_file, env = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=changelog_present, changelog_body=changelog_body,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    client_payload = body["client_payload"]
    assert "changelog" not in client_payload
    assert set(client_payload) == FROZEN_CLIENT_PAYLOAD_KEYS
    expected = _expected_client_payload(env)
    for key, value in expected.items():
        assert client_payload[key] == value


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
@pytest.mark.parametrize(
    "changelog_present, changelog_body",
    [
        pytest.param(False, "", id="file-absent"),
        pytest.param(True, "   \n\t  \n", id="whitespace-only"),
        pytest.param(True, "Real content", id="non-empty"),
    ],
)
def test_warning_and_omission_share_one_condition(
    workflow, changelog_present, changelog_body, monkeypatch, tmp_path, capsys
):
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=changelog_present, changelog_body=changelog_body,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    key_absent = "changelog" not in body["client_payload"]
    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    warning_present = "::warning::" in combined_output
    assert key_absent == warning_present, (
        f"key_absent={key_absent} but warning_present={warning_present} -- "
        "the warning must be printed exactly when the changelog key is omitted"
    )
    if warning_present:
        assert "dispatching without a changelog" in combined_output, (
            "expected the warning message to explain that dispatch is "
            f"proceeding without a changelog, got: {combined_output!r}"
        )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_fetch_step_cannot_fail(workflow):
    fetch_step = _fetch_step(workflow)
    run_text = fetch_step.get("run", "")
    assert "|| true" in run_text, (
        f"expected the {FETCH_STEP_NAME!r} step's run text to contain "
        f"'|| true' so a failed `gh release view` cannot fail the job, in "
        f"{workflow}"
    )
    assert "exit 1" not in run_text, (
        f"the {FETCH_STEP_NAME!r} step's run text must never call `exit 1` "
        f"in {workflow}"
    )
    assert "set -e" not in run_text, (
        f"the {FETCH_STEP_NAME!r} step's run text must never enable "
        f"`set -e` in {workflow}"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_fetch_step_does_not_use_github_output(workflow):
    fetch_step = _fetch_step(workflow)
    run_text = fetch_step.get("run", "")
    assert "$GITHUB_OUTPUT" not in run_text
    assert "id" not in fetch_step, (
        f"{FETCH_STEP_NAME!r} should hand off via CHANGELOG_FILE, not a "
        "step output/id contract"
    )


# ---------------------------------------------------------------------------
# R4 -- an oversized changelog is truncated to ~30 KB with a link
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_oversized_changelog_is_truncated_with_link(workflow, monkeypatch, tmp_path):
    oversized = "a" * 80000
    payload_file, env = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body=oversized,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    changelog = body["client_payload"]["changelog"]
    changelog_bytes = len(changelog.encode("utf-8"))
    assert changelog_bytes < len(oversized.encode("utf-8")), (
        "expected the oversized changelog to be shortened"
    )
    assert changelog_bytes <= TRUNCATE_LIMIT + 300, (
        f"truncated changelog is {changelog_bytes} bytes, expected roughly "
        f"{TRUNCATE_LIMIT} bytes plus a short truncation marker"
    )
    link = f"https://github.com/{env['REPO']}/releases/tag/{env['TAG']}"
    assert link in changelog, f"expected a link to {link!r} in the truncated changelog"

    # The surviving text before the truncation marker must be a genuine
    # ~30000-byte prefix of the original fixture, not e.g. a near-empty
    # string that happens to satisfy the two checks above.
    marker_index = changelog.find("\n\n_")
    assert marker_index != -1, (
        "expected a truncation marker (starting with the double-newline + "
        "underscore separator) appended to the surviving changelog text"
    )
    survived = changelog[:marker_index]
    survived_bytes = len(survived.encode("utf-8"))
    assert survived_bytes >= TRUNCATE_LIMIT - 10, (
        f"expected the surviving prefix to be close to {TRUNCATE_LIMIT} "
        f"bytes, got {survived_bytes} bytes"
    )
    assert oversized.startswith(survived), (
        "expected the surviving text to be a verbatim prefix of the "
        "original changelog"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_truncation_budget_counts_utf8_bytes_not_characters(workflow, monkeypatch, tmp_path):
    # 20000 chars but 60000 bytes (euro sign is 3 bytes in UTF-8). A
    # character-based budget (len(body) > TRUNCATE_LIMIT) would never fire
    # since 20000 <= 30000; a byte-based budget must fire since 60000 > 30000.
    oversized = "€" * 20000
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body=oversized,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    changelog = body["client_payload"]["changelog"]
    changelog_bytes = len(changelog.encode("utf-8"))
    assert changelog_bytes < len(oversized.encode("utf-8")), (
        "expected the oversized (60000-byte, 20000-char) changelog to be "
        "truncated -- if the budget were character-based, 20000 chars would "
        "be under the 30000 limit and no truncation would happen"
    )
    assert changelog_bytes <= TRUNCATE_LIMIT + 300, (
        f"truncated changelog is {changelog_bytes} bytes, expected roughly "
        f"{TRUNCATE_LIMIT} bytes plus a short truncation marker"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_truncation_never_emits_invalid_utf8(workflow, monkeypatch, tmp_path):
    # 3-bytes-per-character fixture, with a leading single ASCII byte so the
    # byte-offset cut at TRUNCATE_LIMIT (30000) lands mid-codepoint: 1 ASCII
    # byte + 29999 bytes of euro signs = 9999 whole euros (29997 bytes) plus
    # 2 dangling bytes of the 10000th euro sign -- unless truncation decodes
    # with errors="ignore".
    oversized = "x" + "€" * 20000  # euro sign: 3 bytes each in UTF-8
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body=oversized,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    changelog = body["client_payload"]["changelog"]
    assert "�" not in changelog, (
        "truncation must decode with errors='ignore', not leave replacement "
        "characters from a mid-codepoint cut"
    )
    changelog.encode("utf-8")  # must still be valid, re-encodable UTF-8


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_changelog_just_under_limit_is_untouched(workflow, monkeypatch, tmp_path):
    exact = "a" * TRUNCATE_LIMIT
    payload_file, env = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body=exact,
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    changelog = body["client_payload"]["changelog"]
    assert changelog == exact, (
        "a changelog exactly at the truncation limit must be passed through "
        "verbatim, with no truncation marker appended"
    )


# ---------------------------------------------------------------------------
# R7 -- JSON serialization doesn't inflate non-ASCII changelog byte size
# (review round 2, finding 2: json.dump's default ensure_ascii=True escapes
# every non-ASCII codepoint to \uXXXX, up to ~3x byte blow-up, which can
# blow the truncation budget's byte-size assumption even though the raw
# body was truncated to stay under TRUNCATE_LIMIT).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_payload_json_is_not_ascii_escaped(workflow, monkeypatch, tmp_path):
    changelog_body = "café 中文 \U0001F600"  # café, CJK, emoji
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body=changelog_body,
    )
    raw_bytes = payload_file.read_bytes()
    assert changelog_body.encode("utf-8") in raw_bytes, (
        "expected the non-ASCII changelog text to appear as raw UTF-8 bytes "
        "in the payload file, not escaped as \\uXXXX sequences"
    )
    assert b"\\u00e9" not in raw_bytes and b"\\u4e2d" not in raw_bytes, (
        "payload file must not contain \\uXXXX escapes for non-ASCII "
        "changelog content -- json.dump must be called with ensure_ascii=False"
    )
    # still valid, round-trippable JSON
    body = json.loads(raw_bytes.decode("utf-8"))
    assert body["client_payload"]["changelog"] == changelog_body


# ---------------------------------------------------------------------------
# R5 -- dispatch.yml declares the permissions its new gh call needs
# ---------------------------------------------------------------------------

def test_dispatch_workflow_declares_contents_read_permission():
    workflow = _load_workflow(DISPATCH_YML)
    assert workflow.get("permissions") == {"contents": "read"}, (
        f"expected dispatch.yml's top-level permissions to be "
        f"{{'contents': 'read'}}, got {workflow.get('permissions')!r}"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_fetch_step_has_gh_token(workflow):
    fetch_env = _fetch_step(workflow).get("env", {})
    assert "GH_TOKEN" in fetch_env, (
        f"expected a GH_TOKEN entry in the {FETCH_STEP_NAME!r} step's env: "
        f"mapping of {workflow}"
    )
    assert fetch_env["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}", (
        f"expected the {FETCH_STEP_NAME!r} step's GH_TOKEN to be "
        f"'${{{{ secrets.GITHUB_TOKEN }}}}', got {fetch_env['GH_TOKEN']!r} "
        f"in {workflow}"
    )


def test_release_workflow_still_has_contents_write():
    workflow = _load_workflow(RELEASE_YML)
    assert workflow.get("permissions") == {"contents": "write"}, (
        "regression guard -- release.yml's existing write scope must not be "
        "weakened by this ticket"
    )


# ---------------------------------------------------------------------------
# R6 -- curl posts exactly the file the builder writes, unchanged endpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_curl_reads_the_file_the_builder_writes(workflow, monkeypatch, tmp_path):
    # (1) text layer: PAYLOAD_FILE is defined in env:.
    env_map = _dispatch_step(workflow).get("env", {})
    assert "PAYLOAD_FILE" in env_map, (
        f"expected a PAYLOAD_FILE entry in the {DISPATCH_STEP_NAME!r} "
        f"step's env: mapping of {workflow}"
    )

    # (2) exec layer: the builder's only write target is PAYLOAD_FILE.
    payload_file, _ = _run_builder(workflow, monkeypatch, tmp_path, changelog_present=False)
    assert payload_file.exists(), "expected PAYLOAD_FILE to exist after the builder ran"
    json.loads(payload_file.read_text(encoding="utf-8"))  # must parse as JSON

    # (3) text layer: curl reads exactly that file, and no other -d.
    run_text = _dispatch_step(workflow).get("run", "")
    assert '-d @"$PAYLOAD_FILE"' in run_text
    dash_d_occurrences = run_text.count(" -d ")
    assert dash_d_occurrences == 1, (
        f"expected exactly one `-d` argument in the curl invocation, found "
        f"{dash_d_occurrences}"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_curl_target_and_headers_unchanged(workflow):
    run_text = _dispatch_step(workflow).get("run", "")
    assert CURL_URL in run_text
    assert CURL_FLAGS in run_text
    assert AUTH_HEADER in run_text
    assert ACCEPT_HEADER in run_text


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_no_heredoc_remains_in_dispatch_step(workflow):
    run_text = _dispatch_step(workflow).get("run", "")
    assert "-d @-" not in run_text
    assert "<<EOF" not in run_text


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_changelog_file_path_is_shared_between_steps(workflow):
    fetch_env = _fetch_step(workflow).get("env", {})
    dispatch_env = _dispatch_step(workflow).get("env", {})
    assert "CHANGELOG_FILE" in fetch_env, (
        f"expected CHANGELOG_FILE in the {FETCH_STEP_NAME!r} step's env: "
        f"mapping of {workflow}"
    )
    assert "CHANGELOG_FILE" in dispatch_env, (
        f"expected CHANGELOG_FILE in the {DISPATCH_STEP_NAME!r} step's env: "
        f"mapping of {workflow}"
    )
    assert fetch_env["CHANGELOG_FILE"] == dispatch_env["CHANGELOG_FILE"]


# ---------------------------------------------------------------------------
# Step ordering -- "fetch before dispatch" (both workflows) and "fetch after
# the release exists" (release.yml only, since dispatch.yml never creates a
# release -- it re-dispatches an already-existing tag).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_changelog_is_fetched_before_dispatch(workflow):
    job_name = JOB_NAME[workflow]
    fetch_index = _step_index(workflow, job_name, FETCH_STEP_NAME)
    dispatch_index = _step_index(workflow, job_name, DISPATCH_STEP_NAME)
    assert fetch_index < dispatch_index, (
        f"expected {FETCH_STEP_NAME!r} (index {fetch_index}) to precede "
        f"{DISPATCH_STEP_NAME!r} (index {dispatch_index}) in job {job_name!r} "
        f"of {workflow}"
    )


def test_release_workflow_fetches_changelog_after_release_is_created():
    job_name = JOB_NAME[RELEASE_YML]
    release_index = _step_index(RELEASE_YML, job_name, RELEASE_CREATE_STEP_NAME)
    fetch_index = _step_index(RELEASE_YML, job_name, FETCH_STEP_NAME)
    assert release_index < fetch_index, (
        f"expected {RELEASE_CREATE_STEP_NAME!r} (index {release_index}) to "
        f"precede {FETCH_STEP_NAME!r} (index {fetch_index}) in job "
        f"{job_name!r} of {RELEASE_YML} -- the changelog must be fetched "
        "from the release that was just created, not before it exists"
    )

    # dispatch.yml is the manual re-dispatch workflow for an already-existing
    # tag -- it never creates a release, so this ordering contract has no
    # referent there.
    dispatch_job = _load_workflow(DISPATCH_YML)["jobs"][JOB_NAME[DISPATCH_YML]]
    step_names = {step.get("name") for step in dispatch_job["steps"]}
    assert RELEASE_CREATE_STEP_NAME not in step_names, (
        f"{DISPATCH_YML} unexpectedly contains a {RELEASE_CREATE_STEP_NAME!r} "
        "step -- this ordering contract is documented as release.yml-only "
        "because dispatch.yml re-dispatches an existing tag rather than "
        "creating a release; if that changed, this test needs revisiting"
    )


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------

def test_both_workflows_build_the_same_request_body(monkeypatch, tmp_path):
    shared_env = dict(DEFAULT_ENV)
    changelog_body = "## Same input, same output\n"

    release_dir = tmp_path / "release"
    dispatch_dir = tmp_path / "dispatch"
    release_dir.mkdir()
    dispatch_dir.mkdir()

    release_payload, _ = _run_builder(
        RELEASE_YML, monkeypatch, release_dir,
        env_overrides=shared_env, changelog_present=True, changelog_body=changelog_body,
    )
    dispatch_payload, _ = _run_builder(
        DISPATCH_YML, monkeypatch, dispatch_dir,
        env_overrides=shared_env, changelog_present=True, changelog_body=changelog_body,
    )

    release_body = json.loads(release_payload.read_text(encoding="utf-8"))
    dispatch_body = json.loads(dispatch_payload.read_text(encoding="utf-8"))
    assert release_body == dispatch_body, (
        "the two workflows' payload builders must be kept in lockstep -- "
        f"release.yml produced {release_body!r}, dispatch.yml produced "
        f"{dispatch_body!r}"
    )


def test_both_workflows_bind_the_same_dispatch_env_keys():
    release_env = set(_dispatch_step(RELEASE_YML).get("env", {}))
    dispatch_env = set(_dispatch_step(DISPATCH_YML).get("env", {}))
    assert release_env == FROZEN_ENV_KEYS, (
        f"release.yml's {DISPATCH_STEP_NAME!r} step env: keys are "
        f"{sorted(release_env)}, expected {sorted(FROZEN_ENV_KEYS)}"
    )
    assert dispatch_env == FROZEN_ENV_KEYS, (
        f"dispatch.yml's {DISPATCH_STEP_NAME!r} step env: keys are "
        f"{sorted(dispatch_env)}, expected {sorted(FROZEN_ENV_KEYS)}"
    )


@pytest.mark.parametrize("workflow", BOTH_WORKFLOWS, ids=WORKFLOW_IDS)
def test_client_payload_has_at_most_ten_properties(workflow, monkeypatch, tmp_path):
    payload_file, _ = _run_builder(
        workflow, monkeypatch, tmp_path,
        changelog_present=True, changelog_body="Some notes",
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert len(body["client_payload"]) <= 10
