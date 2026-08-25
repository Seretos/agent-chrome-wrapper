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


def _fold_continuation_lines_with_sources(text):
    """Like `_join_continuation_lines`, but also returns, for each returned
    logical line, the (start_index, end_index) span (indexes into
    `text.splitlines()`) of the physical lines it was folded from --
    `start_index` is the FIRST physical line, `end_index` the LAST
    (identical for a line that folded with nothing). `_direct_redirect_lines`
    checks `end_index` to see whether, by the time the physical line
    carrying the actual redirect text was reached, an unclosed
    `$(...)`/backtick wrapper was already open -- information the folded
    text alone can't carry, since folding only merges backslash-
    continuations and a wrapper's opening character need not be
    backslash-continued into the line(s) it wraps. `end_index` (not
    `start_index`) is the one that matters: a wrapper opener can itself
    begin on the fold's own first physical line (e.g. split across a
    continuation boundary) and only become "unclosed as of" a later
    physical line within the very same fold -- checking `start_index` alone
    would miss that, since by definition nothing precedes a fold's own
    first line.

    Continuation semantics (shell-accurate): a physical line is folded into
    the next one only when its LITERAL LAST CHARACTER (before the line
    ending) is `\\` -- a backslash followed by trailing whitespace is NOT a
    continuation in real bash, and is therefore NOT folded here either.
    Chains of 3+ physical lines fold into a single logical line.

    Whitespace: the following line's content is preserved as-is, including
    its own leading indentation -- joining does NOT strip the continuation
    line's indentation before appending it (a deliberate, documented choice).

    Terminal-backslash contract: if the LAST physical line ends in a
    trailing backslash with nothing left to fold into, the backslash is
    stripped and the line is still emitted, unfolded -- no exception, no
    dropped line, no dangling `\\` in the returned string.

    `""` returns `[]`.
    """
    if text == "":
        return []
    physical_lines = text.splitlines()
    logical_lines = []
    buffer = None
    buffer_start = None
    buffer_end = None
    for index, line in enumerate(physical_lines):
        if line.endswith("\\"):
            folded = line[:-1]
            if buffer is None:
                buffer = folded
                buffer_start = index
            else:
                buffer = buffer + " " + folded
            buffer_end = index
        else:
            if buffer is None:
                logical_lines.append((line, index, index))
            else:
                logical_lines.append((buffer + " " + line, buffer_start, index))
                buffer = None
                buffer_start = None
                buffer_end = None
    if buffer is not None:
        # Terminal backslash with nothing left to fold into -- still
        # emitted, unfolded, no dangling backslash.
        logical_lines.append((buffer, buffer_start, buffer_end))
    return logical_lines


def _join_continuation_lines(text):
    """Fold shell backslash line-continuations into logical lines.

    See `_fold_continuation_lines_with_sources` for the full contract; this
    is that function with the per-logical-line source span dropped, kept
    as the plain string-list interface existing callers/tests use.
    """
    return [line for line, _, _ in _fold_continuation_lines_with_sources(text)]


def _wrapper_open_before_lines(run_text):
    """For each physical line of `run_text.splitlines()` (by index), whether
    that line begins already inside an unclosed `$(...)` command
    substitution or an unclosed backtick pair opened earlier in `run_text`.

    A SINGLE pass over the FULL `run_text` string (round 2 fix) -- not
    `run_text.splitlines()` scanned line-by-line in isolation -- tracking
    `$(`/`)` paren-depth, backtick parity, single-/double-quote state and
    `#`-comment state together, character by character, so state carries
    correctly across physical-line boundaries. This structurally closes two
    round-2 review findings at once:

    - A wrapper opener whose two characters (`$` and `(`) land on different
      physical lines -- e.g. because a backslash-continuation sits between
      them -- is still recognized as one atomic opener, the same way real
      bash splices a `\\\\\n` away before tokenizing (this is the mirror
      image of `_join_continuation_lines`'s already-established
      continuation folding: there the split was in the INVOCATION, here it
      is in the WRAPPER's own opening token). A per-physical-line scan
      bounded by each line's own length can never see this, since the
      second character never appears within the first line's own index
      range.
    - `$(`/backtick appearing inside a `'single-quoted'` string, or after an
      unescaped `#` comment marker, has NO special meaning and does not
      affect the tracked depth/parity -- matching real bash. A `$(`/backtick
      inside a `"double-quoted"` string RETAINS its special meaning (bash
      still performs command substitution inside double quotes), so
      double-quoted regions are scanned for `$(`/`)`/backtick exactly like
      bare, unquoted text; only `"` itself behaves differently there (it
      CLOSES the region instead of opening one, and `#` does not start a
      comment). A backslash (outside single quotes, including inside double
      quotes) escapes the next character for exactly one character, EXCEPT
      when that next character is a newline -- that's a shell
      line-continuation instead: no state toggle of its own, but the
      newline is still a real physical-line boundary (`run_text.splitlines()`
      still treats it as a new physical line), so it still gets its own
      `open_before` entry.

    Deliberately unhandled, documented simplifications (not needed for
    these workflows' simple `run:` blocks): here-docs (`<<`), ANSI-C
    quoting (`$'...'`), a `\\\n` continuation occurring *inside* a
    single-quoted string (real bash: not a continuation there, still two
    literal characters -- this scanner does not special-case that
    pathological overlap), and other exotic shell lexing. Comment state is
    reset at every `\n` crossed -- continuation or plain -- a simpler,
    deliberately chosen behaviour rather than modelling comment survival
    across a spliced line.
    """
    depth = 0
    backtick_open = False
    in_single = False
    in_double = False
    in_comment = False
    open_before = [depth > 0 or backtick_open]
    i = 0
    n = len(run_text)
    while i < n:
        ch = run_text[i]

        if ch == "\n":
            in_comment = False
            open_before.append(depth > 0 or backtick_open)
            i += 1
            continue

        if in_single:
            # Fully literal in here: only a closing `'` has any meaning.
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_comment:
            i += 1
            continue

        if ch == "\\" and i + 1 < n and run_text[i + 1] == "\n":
            # Line continuation: no state toggle of its own, but the
            # newline is still a real physical-line boundary.
            open_before.append(depth > 0 or backtick_open)
            i += 2
            continue

        if ch == "\\":
            # Escapes the next character for exactly one character (outside
            # single quotes, including inside double quotes).
            i += 2 if i + 1 < n else 1
            continue

        if not in_double:
            if ch == "'":
                in_single = True
                i += 1
                continue
            if ch == '"':
                in_double = True
                i += 1
                continue
            if ch == "#":
                in_comment = True
                i += 1
                continue
        elif ch == '"':
            in_double = False
            i += 1
            continue

        # Common `$(` / `)` / backtick handling -- identical whether bare
        # or inside a double-quoted string.
        if ch == "$":
            # Look past any backslash-newline continuation(s) spliced
            # between `$` and `(`, so a wrapper opener split across a
            # continuation boundary is still recognized as one token.
            j = i + 1
            skipped_newlines = 0
            while j + 1 < n and run_text[j] == "\\" and run_text[j + 1] == "\n":
                j += 2
                skipped_newlines += 1
            if j < n and run_text[j] == "(":
                depth += 1
                for _ in range(skipped_newlines):
                    open_before.append(depth > 0 or backtick_open)
                i = j + 1
                continue
        elif ch == ")" and depth > 0:
            depth -= 1
        elif ch == "`":
            backtick_open = not backtick_open

        i += 1

    physical_line_count = len(run_text.splitlines())
    return open_before[:physical_line_count]


def _direct_redirect_lines(run_text):
    """Return every logical line (continuations folded) that co-occurs a
    `gh release view` invocation and a direct `> "$CHANGELOG_FILE"`
    redirect.

    Applies NO guards beyond that co-occurrence check plus one exception:
    a logical line is excluded if, by the time the physical line it ENDS on
    was reached, that line was already inside an unclosed `$(...)`/backtick
    wrapper -- that's the signature of a command-substitution round-trip
    whose invocation and redirect were folded onto the same logical line by
    coincidence of backslash-continuation, not because the redirect is
    actually direct. Checking the fold's END index (not its start) is what
    catches a wrapper opener that begins on the fold's own first physical
    line (e.g. split across a continuation boundary, see
    `_wrapper_open_before_lines`) and only becomes unclosed-as-of a later
    physical line within that same fold -- the start index alone is always
    `False` there, since nothing precedes a fold's own first line. In
    particular this function still does NOT reject a `$(` or backtick that
    appears WITHIN the returned line's own text (that guard stays the
    caller's responsibility, exactly as it is today in
    `test_fetch_step_redirects_gh_stdout_to_changelog_file`).
    """
    open_before = _wrapper_open_before_lines(run_text)
    result = []
    for line, start_index, end_index in _fold_continuation_lines_with_sources(run_text):
        if "gh release view" not in line or '> "$CHANGELOG_FILE"' not in line:
            continue
        if end_index is not None and open_before[end_index]:
            continue
        result.append(line)
    return result


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
    # The invocation and its redirect must co-occur on the SAME logical line
    # (continuations folded): any command-substitution wrapper (single- or
    # multi-line, e.g. `BODY=$(gh release view ...)` or a multi-line
    # `BODY=$(\n  gh release view ...\n)`) necessarily separates the
    # invocation from a direct `> "$CHANGELOG_FILE"` redirect onto different
    # lines (or removes the direct redirect from the invocation's line
    # entirely), so requiring same-logical-line co-occurrence is what
    # actually pins down "no round-trip through a variable" -- checking
    # `gh release view` lines and the `> "$CHANGELOG_FILE"` redirect's
    # presence in the step independently (as before) is satisfied by a
    # multi-line command substitution whose redirect lives elsewhere in the
    # step. `_direct_redirect_lines` folds backslash line-continuations
    # first, so a `gh release view ... \` + continuation-line redirect that
    # is behaviourally a single shell command is still recognized.
    direct_redirect_lines = _direct_redirect_lines(run_text)
    assert direct_redirect_lines, (
        f"expected a `gh release view` invocation and its "
        f'`> "$CHANGELOG_FILE"` redirect on the SAME logical line in the '
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
# ticket #34 (revision 2) -- harden the direct-redirect heuristic above so a
# backslash-continued (but behaviourally identical) `gh release view ... >
# "$CHANGELOG_FILE"` invocation is still recognized, without weakening the
# anti-round-trip guard against a multi-line `$(...)` command substitution.
#
# `_join_continuation_lines` / `_direct_redirect_lines` do not exist yet --
# these two driving tests are expected to fail with a NameError until
# phase=implement adds them.
# ---------------------------------------------------------------------------

def test_direct_redirect_lines_folds_backslash_continuation():
    # The real fetch command, split after `--json body` with a trailing
    # backslash continuation -- behaviourally identical to the single-line
    # form, and must still be recognized as a direct redirect.
    run_text = (
        'gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body \\\n'
        "  -q '.body // empty' > \"$CHANGELOG_FILE\" 2>/dev/null || true\n"
    )
    result = _direct_redirect_lines(run_text)
    assert len(result) == 1, (
        f"expected the two continuation-joined physical lines to fold into "
        f"exactly one logical line, got {result!r}"
    )
    # Built from three explicit pieces to show exactly where the join
    # inserts its one space: the folded first line (which keeps its own
    # trailing space from before the stripped backslash) + the join
    # separator + the continuation line verbatim (including its own
    # leading indentation, per the documented no-strip contract).
    expected_joined = (
        'gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body '
        + " "
        + "  -q '.body // empty' > \"$CHANGELOG_FILE\" 2>/dev/null || true"
    )
    assert result[0] == expected_joined, (
        f"expected the folded logical line to be the exact single-space-"
        f"joined text with no trailing backslash, got {result[0]!r}"
    )


def test_direct_redirect_lines_rejects_multiline_command_substitution():
    # A multi-line `$(...)` command substitution that round-trips gh's
    # stdout through a variable before redirecting it elsewhere -- no
    # trailing backslashes involved, so an over-eager folding implementation
    # (e.g. one that joins on `$(`...`)` balance rather than on trailing
    # `\`) would wrongly recognize this as a direct redirect.
    run_text = (
        "BODY=$(\n"
        '  gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body '
        "-q '.body // empty'\n"
        ")\n"
        "printf '%s' \"$BODY\" > \"$CHANGELOG_FILE\"\n"
    )
    assert _direct_redirect_lines(run_text) == [], (
        "expected no logical line to co-occur 'gh release view' and "
        '\'> "$CHANGELOG_FILE"\' when they are separated by a multi-line '
        "command substitution"
    )


# ---------------------------------------------------------------------------
# ticket #34 (revision 2) -- edge-case coverage for `_join_continuation_lines`
# / `_direct_redirect_lines`. These may pass immediately once the helpers
# above exist -- expected and acceptable per the plan (only the two driving
# tests above need a RED->GREEN transition).
# ---------------------------------------------------------------------------

def test_join_continuation_lines_folds_chained_continuations():
    text = "one \\\ntwo \\\nthree\n"
    assert _join_continuation_lines(text) == ["one  two  three"]


def test_join_continuation_lines_keeps_trailing_backslash_final_line():
    text = "one\ntwo \\"
    result = _join_continuation_lines(text)
    assert result == ["one", "two "], (
        f"expected the last line's trailing backslash to be stripped and the "
        f"line still emitted, unfolded, got {result!r}"
    )
    assert "\\" not in result[-1], (
        f"expected no dangling backslash in the emitted last line, got "
        f"{result[-1]!r}"
    )


def test_join_continuation_lines_empty_text():
    assert _join_continuation_lines("") == []


def test_join_continuation_lines_does_not_fold_backslash_then_whitespace():
    # Real shell semantics: a backslash is only a line continuation when it
    # is the LITERAL LAST CHARACTER before the newline. A `\` followed by
    # trailing whitespace is NOT a continuation in real bash -- the line is
    # not continued, so "one \   " and "two" must remain two separate
    # logical lines, each preserved verbatim (including "one"'s own
    # trailing whitespace after the backslash).
    text = "one \\   \ntwo\n"
    assert _join_continuation_lines(text) == ["one \\   ", "two"], (
        "a backslash followed by trailing whitespace is not a real shell "
        "continuation and must not be folded"
    )


def test_join_continuation_lines_folds_when_backslash_is_the_true_last_character():
    # Contrast case: the backslash IS the literal last character (no
    # trailing whitespace after it) -- this is a true continuation and must
    # still be folded, even though the line has other content before the
    # backslash.
    text = "one two\\\nthree\n"
    assert _join_continuation_lines(text) == ["one two three"]


def test_direct_redirect_lines_rejects_backslash_continued_invocation_inside_command_substitution():
    # Round 3 review finding 1: the wrapper's opener (`BODY=$(`) sits on its
    # own separate, unfolded physical line (no trailing `\`, so
    # `_join_continuation_lines` never merges it into the invocation's
    # logical line). The invocation itself IS backslash-continued, so after
    # folding, the returned logical line contains neither `$(` nor a
    # backtick -- the caller's existing `"$(" not in line` guard never sees
    # the `$(` because it lives on a different (unfolded) line. This is the
    # already-fixed multi-line-substitution defect class reintroduced
    # through a new path; `_direct_redirect_lines` must reject it by
    # tracking wrapper-open state across the full physical-line sequence,
    # not just within each folded logical line's own text.
    run_text = (
        "BODY=$(\n"
        '  gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body \\\n'
        "    -q '.body // empty' > \"$CHANGELOG_FILE\"\n"
        ")\n"
    )
    assert _direct_redirect_lines(run_text) == [], (
        "expected no direct-redirect line when the folded invocation began "
        "life inside an unclosed $(...) wrapper opened on an earlier, "
        "unrelated physical line"
    )


def test_direct_redirect_lines_returns_guard_failing_line_unfiltered():
    # The helper itself applies no $( guard -- that stays the caller's job.
    # Observable form of the no-guards return contract.
    run_text = 'gh release view "$TAG" $(echo x) > "$CHANGELOG_FILE"\n'
    assert _direct_redirect_lines(run_text) == [run_text.rstrip("\n")]


def test_direct_redirect_lines_returns_backtick_guard_failing_line_unfiltered():
    # Backtick counterpart of the `$(` fixture above.
    run_text = 'gh release view "$TAG" `echo x` > "$CHANGELOG_FILE"\n'
    assert _direct_redirect_lines(run_text) == [run_text.rstrip("\n")]


# ---------------------------------------------------------------------------
# ticket #34 (revision 2, round 2 fix) -- `_wrapper_open_before_lines` is now
# a single quote-aware pass over the FULL run_text, replacing round 1's
# per-physical-line depth/backtick tracker. These tests cover the three
# round-2 review findings: (1)/(2) a `$(` inside a single-quoted string or a
# `#` comment must not poison later lines, and (3) the wrapper's own `$(`
# opener can itself be split across a backslash-continuation boundary. A
# fourth guards against overcorrecting (1)/(2) into suppressing `$(` inside
# DOUBLE-quoted strings, where it must still count.
# ---------------------------------------------------------------------------

def test_wrapper_open_before_ignores_dollar_paren_inside_single_quotes():
    # Round 2 finding 1: real bash treats everything inside a single-quoted
    # string as 100% literal -- a `$(` there must not poison the depth
    # counter for a genuine direct-redirect line that follows.
    run_text = (
        "echo 'literal $( not a wrapper'\n"
        'gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body '
        "-q '.body // empty' > \"$CHANGELOG_FILE\"\n"
    )
    result = _direct_redirect_lines(run_text)
    assert len(result) == 1, (
        "expected the single-quoted `$(` on the earlier line to have no "
        f"effect on the later genuine direct-redirect line, got {result!r}"
    )
    assert "gh release view" in result[0]


def test_wrapper_open_before_ignores_dollar_paren_inside_comment():
    # Round 2 finding 1 (comment variant): a `#`-started comment runs to the
    # end of the line and its contents (including a stray `$(`) have no
    # shell meaning -- must not poison a later genuine direct-redirect line.
    run_text = (
        "# not $( a wrapper\n"
        'gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body '
        "-q '.body // empty' > \"$CHANGELOG_FILE\"\n"
    )
    result = _direct_redirect_lines(run_text)
    assert len(result) == 1, (
        "expected the commented-out `$(` on the earlier line to have no "
        f"effect on the later genuine direct-redirect line, got {result!r}"
    )
    assert "gh release view" in result[0]


def test_wrapper_open_before_detects_dollar_paren_split_by_continuation():
    # Round 2 finding 3: the wrapper's own opening `$(` has its two
    # characters split across a backslash-continuation boundary (`$` ends
    # the first physical line via `\`, `(` starts the next). Real bash
    # splices the `\<newline>` away before tokenizing, so this is still one
    # atomic `$(` opener -- the mirror image of round 1's already-fixed
    # split-invocation bug, this time in the wrapper's own opening token.
    # The redirect ends up on the SAME fold as the split opener (the `$\`
    # ending forces `_join_continuation_lines` to fold the next physical
    # line into it), so this also exercises that the exclusion check must
    # look at the fold's END, not its start.
    run_text = (
        "BODY=$\\\n"
        '(gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body '
        "-q '.body // empty' > \"$CHANGELOG_FILE\"\n"
        ")\n"
    )
    assert _direct_redirect_lines(run_text) == [], (
        "expected no direct-redirect line when the wrapper's own opening "
        "`$(` is itself split across a backslash-continuation boundary"
    )


def test_wrapper_open_before_still_detects_dollar_paren_inside_double_quotes():
    # Safety net for the fix above: quote-awareness must not overcorrect --
    # real bash still performs command substitution inside a "double-quoted"
    # string, so a `$(` there must still count as a real, unclosed opener
    # and still reject the round-trip.
    run_text = (
        'BODY="$(unrelated\n'
        'gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json body '
        "-q '.body // empty' > \"$CHANGELOG_FILE\"\n"
    )
    assert _direct_redirect_lines(run_text) == [], (
        "expected a `$(` inside a double-quoted string to still count as a "
        "real wrapper opener and reject the round-trip"
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
