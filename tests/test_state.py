"""Tests for chrome_wrapper_plugin.state.

All tests use monkeypatching and tmp_path — no real Chrome, no real registry.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import chrome_wrapper_plugin.state as state_module
from chrome_wrapper_plugin.state import (
    MAX_SESSION_SLOTS,
    SessionState,
    claim_session_slot,
    delete_state,
    is_process_alive,
    load_state,
    reap_orphans,
    resolve_session_id,
    save_state,
)


# ── resolve_session_id ────────────────────────────────────────────────────────

def test_session_id_uses_claude_code_session_id(monkeypatch):
    """CLAUDE_CODE_SESSION_ID — the var Claude Code actually sets — wins."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "f16500a2-code")
    assert resolve_session_id() == "f16500a2-code"


def test_session_id_env_var_priority(monkeypatch):
    """CLAUDE_CODE_SESSION_ID wins over all other env-vars."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-code-abc")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-abc")
    monkeypatch.setenv("ANTHROPIC_SESSION_ID", "anthropic-abc")
    monkeypatch.setenv("MCP_SESSION_ID", "mcp-abc")
    assert resolve_session_id() == "claude-code-abc"


def test_session_id_fallback_chain(monkeypatch):
    """CLAUDE_SESSION_ID is used when CLAUDE_CODE_SESSION_ID is absent."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-abc")
    monkeypatch.setenv("ANTHROPIC_SESSION_ID", "anthropic-abc")
    monkeypatch.setenv("MCP_SESSION_ID", "mcp-abc")
    assert resolve_session_id() == "claude-abc"


def test_session_id_fallback_chain_anthropic(monkeypatch):
    """ANTHROPIC_SESSION_ID is used when both Claude vars are absent."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_SESSION_ID", "anthropic-xyz")
    monkeypatch.setenv("MCP_SESSION_ID", "mcp-xyz")
    assert resolve_session_id() == "anthropic-xyz"


def test_session_id_deterministic_fallback(monkeypatch):
    """Fallback ID is sha256(host:user)[:16] and stable across calls."""
    for var in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "ANTHROPIC_SESSION_ID",
        "MCP_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    key = socket.gethostname() + ":" + getpass.getuser()
    expected = hashlib.sha256(key.encode()).hexdigest()[:16]

    assert resolve_session_id() == expected
    # Stable across calls
    assert resolve_session_id() == expected


# ── _state_dir ────────────────────────────────────────────────────────────────

def test_state_dir_uses_claude_plugin_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    # Access via the module-level function (must reset cached state dir)
    result = state_module._state_dir()
    assert result == tmp_path / "instances"
    assert result.is_dir()


def test_state_dir_falls_back_to_tmpdir(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    result = state_module._state_dir()
    # Should be inside the system temp directory
    assert str(result).startswith(tempfile.gettempdir())
    assert result.name == "instances"


def test_state_dir_not_under_cwd(monkeypatch):
    """State directory must never be inside the current working directory."""
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    result = state_module._state_dir()
    cwd = Path.cwd()
    assert not str(result).startswith(str(cwd)), (
        f"State dir {result!r} must not be under cwd {cwd!r}"
    )


# ── SessionState serialization ────────────────────────────────────────────────

def _make_state(**overrides) -> SessionState:
    defaults = dict(
        session_id="test-session",
        pid=12345,
        port=9222,
        user_data_dir="/tmp/udd",
        profile="/tmp/master",
        created_at="2026-01-01T00:00:00",
    )
    defaults.update(overrides)
    return SessionState(**defaults)


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    st = _make_state(owner_pid=4242)
    save_state(st)
    loaded = load_state("test-session")

    assert loaded is not None
    assert loaded.session_id == st.session_id
    assert loaded.pid == st.pid
    assert loaded.port == st.port
    assert loaded.user_data_dir == st.user_data_dir
    assert loaded.profile == st.profile
    assert loaded.created_at == st.created_at
    assert loaded.owner_pid == 4242


def test_load_legacy_json_without_owner_pid_defaults_to_none(tmp_path, monkeypatch):
    """JSON written before owner_pid existed still loads, with owner_pid None."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "session_id": "legacy-session",
        "pid": 111,
        "port": 9222,
        "user_data_dir": "/tmp/udd",
        "profile": "/tmp/master",
        "created_at": "2026-01-01T00:00:00",
    }
    (instances_dir / "legacy-session.json").write_text(json.dumps(legacy))

    loaded = load_state("legacy-session")
    assert loaded is not None
    assert loaded.owner_pid is None


def test_load_returns_none_for_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    assert load_state("does-not-exist") is None


def test_load_handles_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    # Write invalid JSON directly
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    (instances_dir / "bad-session.json").write_text("not json!!!")
    result = load_state("bad-session")
    assert result is None


# ── is_process_alive ──────────────────────────────────────────────────────────

def test_is_process_alive_current_process():
    """The current process is alive."""
    assert is_process_alive(os.getpid()) is True


def test_is_process_alive_dead_pid():
    """A dead PID (no such process) returns False.

    On Windows, OpenProcess returns a null handle for a non-existent PID with
    GetLastError() == ERROR_INVALID_PARAMETER (87).  We simulate this via
    ctypes mocking so the test is deterministic and not subject to the Windows
    behaviour where a PID remains queryable until all OS handles to it are closed
    (including the one Popen holds internally while the Popen object is alive).
    """
    import ctypes

    # Patch windll.kernel32 only when it exists (i.e. on Windows).
    if not hasattr(ctypes, "windll"):
        pytest.skip("Windows-only test")

    ERROR_INVALID_PARAMETER = 87  # GetLastError value for non-existent PID

    with (
        mock.patch.object(
            ctypes.windll.kernel32, "OpenProcess", return_value=None
        ),
        mock.patch.object(
            ctypes.windll.kernel32,
            "GetLastError",
            return_value=ERROR_INVALID_PARAMETER,
        ),
    ):
        assert is_process_alive(999999999) is False


# ── reap_orphans ──────────────────────────────────────────────────────────────

def test_reap_orphans_removes_dead_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    dead_udd = tmp_path / "dead_udd"
    dead_udd.mkdir()
    dead_state = _make_state(session_id="dead-session", pid=999999999, user_data_dir=str(dead_udd))
    save_state(dead_state)

    with mock.patch.object(state_module, "is_process_alive", return_value=False):
        reaped = reap_orphans()

    assert "dead-session" in reaped
    assert not (tmp_path / "instances" / "dead-session.json").exists()
    assert not dead_udd.exists()


def test_reap_orphans_keeps_live_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    live_udd = tmp_path / "live_udd"
    live_udd.mkdir()
    live_state = _make_state(session_id="live-session", pid=os.getpid(), user_data_dir=str(live_udd))
    save_state(live_state)

    with mock.patch.object(state_module, "is_process_alive", return_value=True):
        reaped = reap_orphans()

    assert "live-session" not in reaped
    assert (tmp_path / "instances" / "live-session.json").exists()
    assert live_udd.exists()


def test_reap_orphans_skips_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    (instances_dir / "corrupt.json").write_text("{{broken")

    # Must not raise — corrupt files are skipped
    reaped = reap_orphans()
    assert "corrupt" not in reaped


def _make_placeholder(**overrides) -> SessionState:
    defaults = dict(
        session_id="placeholder-session",
        pid=0,
        port=0,
        user_data_dir="",
        profile="",
        created_at="2026-01-01T00:00:00",
        owner_pid=os.getpid(),
    )
    defaults.update(overrides)
    return SessionState(**defaults)


def test_reap_orphans_keeps_placeholder_with_live_owner(tmp_path, monkeypatch):
    """A reservation placeholder (pid=0) whose owner wrapper is alive is kept.

    is_process_alive(0) would be unsafe (Windows PID 0 == System Idle Process),
    so reap_orphans must judge placeholders by owner_pid, not pid.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    placeholder = _make_placeholder(session_id="mid-launch", owner_pid=os.getpid())
    save_state(placeholder)

    reaped = reap_orphans()

    assert "mid-launch" not in reaped
    assert (tmp_path / "instances" / "mid-launch.json").exists()


def test_reap_orphans_removes_placeholder_with_dead_owner(tmp_path, monkeypatch):
    """A placeholder whose owning wrapper has died is reaped, with no rmtree
    call on the empty user_data_dir (would otherwise be an unguarded rmtree(''))."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    dead_owner_pid = 999999999
    placeholder = _make_placeholder(session_id="abandoned", owner_pid=dead_owner_pid)
    save_state(placeholder)

    with mock.patch.object(state_module, "is_process_alive", return_value=False):
        reaped = reap_orphans()

    assert "abandoned" in reaped
    assert not (tmp_path / "instances" / "abandoned.json").exists()


def test_reap_orphans_removes_placeholder_with_none_owner(tmp_path, monkeypatch):
    """A placeholder with owner_pid=None (should not normally occur, but is
    treated as ownerless) is reaped rather than kept forever."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    placeholder = _make_placeholder(session_id="ownerless", owner_pid=None)
    save_state(placeholder)

    reaped = reap_orphans()

    assert "ownerless" in reaped
    assert not (tmp_path / "instances" / "ownerless.json").exists()


# ── claim_session_slot ────────────────────────────────────────────────────────

def test_claim_session_slot_no_state_reserves_placeholder(tmp_path, monkeypatch):
    """No existing state for base_id -> claims slot 1 (base_id verbatim) and
    leaves an O_EXCL placeholder on disk with pid=0 and owner_pid=self."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    session_id, reattachable = claim_session_slot("base")

    assert session_id == "base"
    assert reattachable is None
    on_disk = load_state("base")
    assert on_disk is not None
    assert on_disk.pid == 0
    assert on_disk.owner_pid == os.getpid()


def test_claim_session_slot_dead_owner_live_chrome_reattaches(tmp_path, monkeypatch):
    """owner_pid dead but Chrome pid alive -> crash recovery: adopt the slot
    and return the state so the caller can reattach to the live Chrome."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    dead_owner_pid = 999999999
    live_chrome_pid = 55555
    existing = _make_state(
        session_id="base", pid=live_chrome_pid, owner_pid=dead_owner_pid
    )
    save_state(existing)

    with mock.patch.object(
        state_module, "is_process_alive",
        side_effect=lambda pid: pid == live_chrome_pid,
    ):
        session_id, reattachable = claim_session_slot("base")

    assert session_id == "base"
    assert reattachable is not None
    assert reattachable.pid == live_chrome_pid
    on_disk = load_state("base")
    assert on_disk.owner_pid == os.getpid()


def test_claim_session_slot_owner_is_self_returns_unchanged(tmp_path, monkeypatch):
    """owner_pid == os.getpid() -> this wrapper re-initialising; state returned
    unchanged (no rewrite needed)."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    existing = _make_state(session_id="base", pid=55555, owner_pid=os.getpid())
    save_state(existing)

    with mock.patch.object(state_module, "is_process_alive", return_value=True):
        session_id, reattachable = claim_session_slot("base")

    assert session_id == "base"
    assert reattachable is not None
    assert reattachable.pid == 55555


def test_claim_session_slot_legacy_json_live_chrome_reattaches(tmp_path, monkeypatch):
    """Legacy JSON with no owner_pid at all, live Chrome pid -> back-compat
    reattach: adopt with owner_pid=self."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "session_id": "base",
        "pid": 55555,
        "port": 9222,
        "user_data_dir": "/tmp/udd",
        "profile": "/tmp/master",
        "created_at": "2026-01-01T00:00:00",
    }
    (instances_dir / "base.json").write_text(json.dumps(legacy))

    with mock.patch.object(state_module, "is_process_alive", return_value=True):
        session_id, reattachable = claim_session_slot("base")

    assert session_id == "base"
    assert reattachable is not None
    assert reattachable.pid == 55555
    on_disk = load_state("base")
    assert on_disk.owner_pid == os.getpid()


def test_claim_session_slot_legacy_json_dead_chrome_reclaims(tmp_path, monkeypatch):
    """Legacy JSON with no owner_pid, dead Chrome pid -> reclaimed as a fresh
    placeholder (caller must launch Chrome)."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "session_id": "base",
        "pid": 99999,
        "port": 9222,
        "user_data_dir": "/tmp/udd",
        "profile": "/tmp/master",
        "created_at": "2026-01-01T00:00:00",
    }
    (instances_dir / "base.json").write_text(json.dumps(legacy))

    with mock.patch.object(state_module, "is_process_alive", return_value=False):
        session_id, reattachable = claim_session_slot("base")

    assert session_id == "base"
    assert reattachable is None
    on_disk = load_state("base")
    assert on_disk.pid == 0


def test_claim_session_slot_corrupt_json_reclaimed(tmp_path, monkeypatch):
    """Corrupt JSON in the slot is reclaimed as a fresh placeholder."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    (instances_dir / "base.json").write_text("{{not valid json")

    session_id, reattachable = claim_session_slot("base")

    assert session_id == "base"
    assert reattachable is None
    on_disk = load_state("base")
    assert on_disk.pid == 0


def test_claim_session_slot_live_foreign_owner_skips_to_next_slot(tmp_path, monkeypatch):
    """base_id's slot is held by a live wrapper that is not us -> the next
    free slot (base-2) is claimed instead."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    foreign_pid = os.getpid() + 1  # guaranteed != os.getpid()
    existing = _make_state(session_id="base", pid=55555, owner_pid=foreign_pid)
    save_state(existing)

    with mock.patch.object(state_module, "is_process_alive", return_value=True):
        session_id, reattachable = claim_session_slot("base")

    assert session_id == "base-2"
    assert reattachable is None


def test_claim_session_slot_two_live_foreign_owners_returns_slot_3(tmp_path, monkeypatch):
    """base and base-2 both held by live foreign wrappers -> base-3 claimed."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    foreign_pid = os.getpid() + 1
    save_state(_make_state(session_id="base", pid=1, owner_pid=foreign_pid))
    save_state(_make_state(session_id="base-2", pid=2, owner_pid=foreign_pid))

    with mock.patch.object(state_module, "is_process_alive", return_value=True):
        session_id, reattachable = claim_session_slot("base")

    assert session_id == "base-3"
    assert reattachable is None


def test_concurrent_adopt_race_never_shares_the_same_slot(tmp_path, monkeypatch):
    """Two wrapper processes racing to adopt the SAME dead-owner/live-Chrome
    slot must end up owning DIFFERENT slots -- never sharing the same Chrome.

    Interleaves a first claimer ("t1", on a background thread) so it pauses
    exactly at the read-decide point (state.owner_pid dead, Chrome pid alive)
    while a second claimer ("t2") races in on the main thread and tries to
    claim the same base_id concurrently, before t1 has written anything back.

    Pre-fix RED (review finding 1): claim_session_slot()'s "owner dead ->
    adopt" branch did a plain read-then-save_state() with no mutual exclusion.
    With this interleaving, t2 observes the identical dead-owner state (t1
    hasn't written yet), decides to adopt too, and returns "base"; t1 then
    resumes and also returns "base". Both claimers end up with the SAME slot
    id -- this test's assertion that the two slot ids differ fails on the
    unfixed code (which has no _acquire_adopt_lock at all).
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    dead_owner_pid = 999999999
    live_chrome_pid = 55555
    existing = _make_state(session_id="base", pid=live_chrome_pid, owner_pid=dead_owner_pid)
    save_state(existing)

    real_is_process_alive = state_module.is_process_alive
    t1_reached_decision = threading.Event()
    release_t1 = threading.Event()
    results: dict = {}

    def _controlled_is_process_alive(pid):
        if pid == dead_owner_pid:
            if threading.current_thread().name == "claim-race-t1":
                t1_reached_decision.set()
                assert release_t1.wait(timeout=5), "test driver never released t1"
            return False
        return real_is_process_alive(pid)

    monkeypatch.setattr(state_module, "is_process_alive", _controlled_is_process_alive)

    def _t1():
        results["t1"] = claim_session_slot("base")

    t1 = threading.Thread(target=_t1, name="claim-race-t1")
    t1.start()
    assert t1_reached_decision.wait(timeout=5), "t1 never reached the decision point"

    # t2 races in on the main thread while t1 is paused mid-decision, holding
    # (on the fixed code) the adopt-lock for "base".
    results["t2"] = claim_session_slot("base")

    release_t1.set()
    t1.join(timeout=5)

    slot_ids = {results["t1"][0], results["t2"][0]}
    assert len(slot_ids) == 2, (
        f"both claimers ended up on slot(s) {slot_ids!r} instead of two "
        "distinct slots -- they now believe they share the same Chrome "
        "instance, exactly the collision ticket #27 fixes"
    )


def test_reserve_placeholder_never_exposes_partial_file(tmp_path, monkeypatch):
    """A concurrent reader of a reservation's target path must never observe
    it in an empty/partial state -- it either doesn't exist yet, or exists
    fully-formed and parseable.

    Pre-fix RED (review finding 2): _reserve_placeholder() called
    os.open(path, O_CREAT | O_EXCL) directly on the FINAL path (creating it
    empty) and only wrote the JSON body in a separate, later os.write() call.
    Pausing right at that os.write() call (as this test does) lets a
    concurrent reader observe the target path already existing but with zero
    bytes -- this test's check that the observed file is never "exists but
    empty" fails on the unfixed code.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    path = tmp_path / "instances" / "race-slot.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    reader_saw_write_step = threading.Event()
    writer_may_finish = threading.Event()
    observed: dict = {}

    real_os_write = os.write

    def _delayed_write(fd, data):
        reader_saw_write_step.set()
        assert writer_may_finish.wait(timeout=5), "reader never signalled"
        return real_os_write(fd, data)

    monkeypatch.setattr(os, "write", _delayed_write)

    def _writer():
        state_module._reserve_placeholder(path, "race-slot", os.getpid())

    t = threading.Thread(target=_writer, name="reserve-writer")
    t.start()
    assert reader_saw_write_step.wait(timeout=5), "writer never reached the write step"

    # While the writer is paused mid-write, observe whatever is currently on
    # disk at the target path.
    if path.exists():
        raw = path.read_bytes()
        if raw:
            observed["state"] = "complete"
            SessionState.from_json(raw.decode("utf-8"))  # must not raise
        else:
            observed["state"] = "empty"  # the bug: exists but zero bytes
    else:
        observed["state"] = "absent"  # fine: nothing to see yet

    writer_may_finish.set()
    t.join(timeout=5)

    assert observed["state"] != "empty", (
        f"the reservation's target path was observed in state "
        f"{observed['state']!r} mid-write -- a concurrent reader hitting "
        "this exact window would raise JSONDecodeError and wrongly reclaim "
        "an in-flight reservation as 'corrupt'"
    )


# ── _publish_if_absent ────────────────────────────────────────────────────────

def test_publish_if_absent_posix_branch_never_overwrites_existing_dest(tmp_path, monkeypatch):
    """Force the POSIX (`os.link`) branch of `_publish_if_absent()` even
    though this suite normally runs on Windows, and verify it gives the same
    only-if-absent guarantee as the Windows `os.rename()` branch (ticket #27
    review finding 3): plain `os.rename()` on POSIX SILENTLY OVERWRITES an
    existing destination (no exception), which would blind-clobber a live
    owner's slot/lock file. `os.link()` must instead refuse, leaving the
    existing destination untouched, and the temp source file must always be
    cleaned up (never left orphaned) whether or not the publish succeeded.
    """
    monkeypatch.setattr(state_module.os, "name", "posix")

    dest = tmp_path / "dest.json"
    dest.write_text("original", encoding="utf-8")

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(tmp_path))
    os.write(tmp_fd, b"new-content")
    os.close(tmp_fd)

    published = state_module._publish_if_absent(tmp_name, dest)

    assert published is False
    assert dest.read_text(encoding="utf-8") == "original", (
        "existing destination was clobbered -- this is the exact blind "
        "overwrite bug class finding 2 closed, now reintroduced via the "
        "POSIX os.rename() fallback"
    )
    assert not Path(tmp_name).exists(), "temp file was left orphaned on failure"


def test_publish_if_absent_posix_branch_publishes_when_dest_absent(tmp_path, monkeypatch):
    """The POSIX branch still succeeds -- and cleans up the temp name --
    when there is no existing destination to protect."""
    monkeypatch.setattr(state_module.os, "name", "posix")

    dest = tmp_path / "dest.json"
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(tmp_path))
    os.write(tmp_fd, b"new-content")
    os.close(tmp_fd)

    published = state_module._publish_if_absent(tmp_name, dest)

    assert published is True
    assert dest.read_text(encoding="utf-8") == "new-content"
    assert not Path(tmp_name).exists()


def test_publish_if_absent_windows_branch_cleans_up_tmp_on_lost_race(tmp_path, monkeypatch):
    """The Windows (`os.rename`) branch must clean up `tmp_path` on a LOST
    race too, not only on the won-race path (ticket #27 review finding 1).

    Pre-fix RED: the `os.name == "nt"` branch had no `finally`/cleanup at
    all -- on `FileExistsError` it just returned False, leaving the temp
    file (e.g. a `.reserve-*.tmp` from `_reserve_placeholder` or a
    `.lock-*.tmp` from `_acquire_adopt_lock`) behind forever, since
    `reap_orphans()` only globs `*.json` and never looks at `*.tmp` files.
    This IS the live production path -- the plugin is Windows-only and this
    suite runs natively on Windows, so `os.name` is already `"nt"` without
    the monkeypatch; the monkeypatch just makes that explicit/robust to
    where the suite runs. This test's assertion that no `*.tmp` file
    remains after a lost race fails on the unfixed code.
    """
    monkeypatch.setattr(state_module.os, "name", "nt")

    dest = tmp_path / "dest.json"
    dest.write_text("original", encoding="utf-8")

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(tmp_path), suffix=".tmp")
    os.write(tmp_fd, b"new-content")
    os.close(tmp_fd)

    published = state_module._publish_if_absent(tmp_name, dest)

    assert published is False
    assert dest.read_text(encoding="utf-8") == "original", (
        "the Windows branch must never overwrite an existing destination"
    )
    assert not Path(tmp_name).exists(), (
        "the temp file was left orphaned on the Windows lost-race path -- "
        "an unbounded leak since reap_orphans() only globs *.json"
    )
    assert list(tmp_path.glob("*.tmp")) == []


def test_acquire_adopt_lock_self_heals_from_crash_in_the_gap(tmp_path, monkeypatch):
    """A lock file left behind empty/unparseable by a holder that crashed
    between creating the file and writing its pid must eventually be
    breakable, so the slot doesn't stay dead-locked forever.

    Pre-fix RED (review finding 1): `_acquire_adopt_lock()` used a two-step
    `os.open(O_CREAT|O_EXCL)` then a separate `os.write()`. A holder that
    died in that gap (OOM kill, forced termination, power loss) left an
    empty `<slot>.json.lock` behind. Every later caller hit `FileExistsError`
    on the create, read `""`, `int("")` raised `ValueError`, and the
    `except (OSError, ValueError): return False` branch fired -- and nothing
    in the code ever revisited that lock afterwards, since the "break a
    stale lock" logic only ran once `holder_pid` parsed successfully. So on
    the unfixed code this test's call to `_acquire_adopt_lock()` returns
    `False` unconditionally, no matter how old the empty lock file is --
    this test's assertion that it eventually returns `True` fails on that
    code.

    On the fixed code, an unparseable lock is only ever a pre-existing
    leftover (a lock we create is now always published complete-or-absent),
    and `_lock_is_unparseable_and_stale()` lets it self-heal once it is
    older than `_LOCK_STALE_SECONDS`.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    session_path = tmp_path / "instances" / "abandoned-lock.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = state_module._lock_path(session_path)
    lock_path.write_bytes(b"")  # left behind mid-write by the crashed holder
    stale_time = time.time() - state_module._LOCK_STALE_SECONDS - 1
    os.utime(lock_path, (stale_time, stale_time))

    acquired = state_module._acquire_adopt_lock(session_path)

    assert acquired is True, (
        "an empty/unparseable lock file was never broken -- the slot is "
        "starved forever, exactly the crash-in-the-gap bug review finding "
        "1 describes"
    )
    # Successfully acquired -> the lock is now this process's, holding our
    # own pid, not left in the broken state.
    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    state_module._release_adopt_lock(session_path)


def test_acquire_adopt_lock_treats_fresh_unparseable_lock_as_live(tmp_path, monkeypatch):
    """An unparseable lock file that is NOT yet stale is treated
    conservatively as a live in-progress write, not broken immediately."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    session_path = tmp_path / "instances" / "fresh-lock.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = state_module._lock_path(session_path)
    lock_path.write_bytes(b"")  # unparseable, but freshly created (mtime now)

    assert state_module._acquire_adopt_lock(session_path) is False
    # Untouched -- still there for a legitimate concurrent holder to finish.
    assert lock_path.exists()


def test_acquire_adopt_lock_break_aborts_if_lock_republished_before_unlink(tmp_path, monkeypatch):
    """A breaker must not delete a lock that was replaced by a fresh,
    live-held lock in the gap between reading the stale content and
    unlinking it (ticket #27 review finding 2).

    Pre-fix RED: both break sites (the unparseable-and-stale branch at
    state.py:342 and the dead-holder branch at state.py:353) called
    `lock_path.unlink()` unconditionally after inspecting the file's
    content, with no re-verification immediately before the unlink. This
    test forces exactly the interleaving the finding describes by hooking
    `is_process_alive()` -- a call that exists identically before and after
    the fix, sitting right between the breaker's content read and its
    eventual unlink. When the breaker asks whether the dead pid it just read
    is alive, this hook pauses it there (having already committed to "dead
    -> break"), lets a second "process" (the main thread) overwrite the same
    lock file with a fresh LIVE pid -- simulating another wrapper
    legitimately republishing the lock in that gap -- then resumes the
    breaker.

    On the unfixed code, the breaker proceeds straight from `is_process_alive
    -> False` to an unconditional `lock_path.unlink()`, deleting the live,
    freshly-republished lock; its retry then succeeds and
    `_acquire_adopt_lock` returns True -- the racing breaker enters the
    critical section concurrently with whoever holds the live lock, exactly
    the dual-adoption failure ticket #27 exists to close. This test's
    assertions that the live lock survives and the breaker does NOT acquire
    fail on that code.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    session_path = tmp_path / "instances" / "contested.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_module._lock_path(session_path)

    dead_pid = 999999999
    live_pid = 424242

    lock_path.write_text(str(dead_pid), encoding="utf-8")
    stale_time = time.time() - state_module._LOCK_STALE_SECONDS - 1
    os.utime(lock_path, (stale_time, stale_time))

    breaker_paused = threading.Event()
    republish_done = threading.Event()

    def _is_process_alive_with_race(pid):
        if pid == dead_pid:
            breaker_paused.set()
            assert republish_done.wait(timeout=5), "republisher never signalled"
            return False
        return pid == live_pid

    monkeypatch.setattr(state_module, "is_process_alive", _is_process_alive_with_race)

    results: dict = {}

    def _breaker():
        results["acquired"] = state_module._acquire_adopt_lock(session_path)

    t = threading.Thread(target=_breaker, name="lock-breaker")
    t.start()
    assert breaker_paused.wait(timeout=5), "breaker never reached the liveness check"

    # Someone else races in and legitimately republishes the lock with a
    # LIVE pid, in the gap between the breaker deciding "dead" and its
    # eventual unlink.
    lock_path.write_text(str(live_pid), encoding="utf-8")

    republish_done.set()
    t.join(timeout=5)

    assert lock_path.exists(), (
        "the live, freshly-republished lock was deleted by a breaker that "
        "inspected a DIFFERENT (stale) lock's content"
    )
    assert lock_path.read_text(encoding="utf-8").strip() == str(live_pid)
    assert results["acquired"] is False, (
        "the racing breaker acquired the lock after deleting a different "
        "process's live lock -- exactly the dual-adoption failure ticket "
        "#27 exists to close"
    )


def test_claim_session_slot_all_slots_full_uses_pid_suffix(tmp_path, monkeypatch):
    """All MAX_SESSION_SLOTS slots held by live foreign owners -> last-resort
    PID-suffixed id, collision-free by construction."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    foreign_pid = os.getpid() + 1
    for i in range(1, MAX_SESSION_SLOTS + 1):
        candidate = "base" if i == 1 else f"base-{i}"
        save_state(_make_state(session_id=candidate, pid=i, owner_pid=foreign_pid))

    with mock.patch.object(state_module, "is_process_alive", return_value=True):
        session_id, reattachable = claim_session_slot("base")

    assert session_id == f"base-{os.getpid()}"
    assert reattachable is None
    on_disk = load_state(session_id)
    assert on_disk is not None
    assert on_disk.pid == 0
