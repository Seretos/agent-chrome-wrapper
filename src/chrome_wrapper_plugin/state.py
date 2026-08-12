# Session ID resolution order:
# 1. CLAUDE_CODE_SESSION_ID — actually set by Claude Code (verified in-process)
# 2. CLAUDE_SESSION_ID   — legacy/generic Claude host env-var
# 3. ANTHROPIC_SESSION_ID — alternative host env-var
# 4. MCP_SESSION_ID      — generic MCP host env-var
# 5. Deterministic fallback: sha256(hostname + ":" + username).hexdigest()[:16]
#    Stable across restarts on the same host+user combination, but IDENTICAL
#    for every concurrent process on the same host+user — see
#    claim_session_slot() for how collisions on this fallback are resolved
#    into distinct owned slots so two wrappers never share one Chrome.

from __future__ import annotations

import ctypes
import ctypes.wintypes
import dataclasses
import datetime
import getpass
import hashlib
import json
import logging
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_session_id() -> str:
    """Return a stable session identifier for this process."""
    for var in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "ANTHROPIC_SESSION_ID",
        "MCP_SESSION_ID",
    ):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    # Deterministic fallback — stable across restarts on same host+user
    key = socket.gethostname() + ":" + getpass.getuser()
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _state_dir() -> Path:
    """Return (and create) the directory where session JSON files are stored."""
    base_env = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if base_env:
        base = Path(base_env)
    else:
        base = Path(tempfile.gettempdir()) / "chrome_wrapper_plugin"
    state_path = base / "instances"
    state_path.mkdir(parents=True, exist_ok=True)
    return state_path


@dataclasses.dataclass
class SessionState:
    session_id: str
    pid: int
    port: int
    user_data_dir: str  # stored as str for JSON round-trip
    profile: str
    created_at: str
    # PID of the *wrapper* process that owns this slot (os.getpid()), not the
    # Chrome pid.  None means legacy JSON written before this field existed.
    # Defaulted so `cls(**data)` in from_json still loads old files unchanged.
    owner_pid: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "SessionState":
        data = json.loads(raw)
        return cls(**data)


def save_state(state: SessionState) -> None:
    """Atomically write state JSON to <_state_dir()>/<session_id>.json."""
    target = _state_dir() / f"{state.session_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(state.to_json(), encoding="utf-8")
    os.replace(tmp, target)


def load_state(session_id: str) -> Optional[SessionState]:
    """Return SessionState for *session_id*, or None if missing or corrupt."""
    path = _state_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return SessionState.from_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("Corrupt session state at %s: %s", path, exc)
        return None


# ── process-liveness check (ctypes WinAPI, no psutil) ──────────────────────

PROCESS_QUERY_INFORMATION = 0x0400
_ERROR_ACCESS_DENIED = 5  # GetLastError() value when process exists but is inaccessible


def is_process_alive(pid: int) -> bool:
    """Return True when *pid* refers to a running process on Windows.

    OpenProcess returns a null handle both when the PID does not exist
    (ERROR_INVALID_PARAMETER / 87) AND when the process exists but the
    caller lacks permission to open it (ERROR_ACCESS_DENIED / 5).  We
    must treat ACCESS_DENIED as *alive* to avoid reaping a live session
    owned by a more-privileged user or a protected system process.
    """
    try:
        _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except AttributeError:
        # Non-Windows (e.g. Linux CI): fall back to POSIX signal-0 check.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # Set correct return/arg types so the handle value is never truncated on
    # 64-bit Windows (HANDLE is pointer-sized; default ctypes restype is c_int).
    _kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,   # dwDesiredAccess
        ctypes.wintypes.BOOL,    # bInheritHandle
        ctypes.wintypes.DWORD,   # dwProcessId
    ]
    _kernel32.GetLastError.restype = ctypes.wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

    handle = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    # Null handle: ERROR_ACCESS_DENIED (5) means the process exists but we
    # cannot open it — still alive.
    return _kernel32.GetLastError() == _ERROR_ACCESS_DENIED


def reap_orphans() -> list[str]:
    """Delete state files (and user-data-dirs) for dead Chrome processes.

    Returns the list of session IDs that were reaped.
    """
    reaped: list[str] = []
    for json_path in _state_dir().glob("*.json"):
        try:
            state = SessionState.from_json(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Skipping corrupt state file %s: %s", json_path, exc)
            continue

        if state.pid <= 0:
            # Reservation placeholder from claim_session_slot() — Chrome has not
            # been launched yet, so is_process_alive(state.pid) is not a safe
            # probe (pid 0 is the Windows System Idle Process; POSIX os.kill(0, 0)
            # signals the whole process group). Judge liveness by owner_pid.
            if state.owner_pid is not None and is_process_alive(state.owner_pid):
                # A wrapper is mid-launch for this slot — keep it. This is what
                # closes the reap-vs-launch race.
                continue
            logger.debug(
                "Reaping stale placeholder %s (owner_pid %s)",
                state.session_id, state.owner_pid,
            )
            try:
                json_path.unlink()
            except OSError:
                pass
            reaped.append(state.session_id)
            continue

        if not is_process_alive(state.pid):
            logger.debug("Reaping orphan session %s (pid %d)", state.session_id, state.pid)
            shutil.rmtree(state.user_data_dir, ignore_errors=True)
            try:
                json_path.unlink()
            except OSError:
                pass
            reaped.append(state.session_id)

    return reaped


MAX_SESSION_SLOTS = 32


def _placeholder(session_id: str, owner_pid: int) -> SessionState:
    """A complete, parseable SessionState marking a slot as reserved.

    pid=0 is the sentinel for "reserved, Chrome not launched yet" — never a
    real Chrome pid, so it can't be mistaken for a live Chrome process.
    """
    return SessionState(
        session_id=session_id,
        pid=0,
        port=0,
        user_data_dir="",
        profile="",
        created_at=datetime.datetime.now().isoformat(),
        owner_pid=owner_pid,
    )


def _publish_if_absent(tmp_path: str, dest: Path) -> bool:
    """Publish the already-complete file at *tmp_path* to *dest*, but only
    if *dest* does not already exist. Returns True once *dest* holds the
    content; False if *dest* already existed, in which case *tmp_path* has
    already been cleaned up by this function.

    This is the "only-if-absent create" primitive both `_reserve_placeholder`
    and `_acquire_adopt_lock` publish through, and it must give the same
    guarantee on every platform tests run on (ticket #27 review finding 3):
    the destination path is never observable as existing-but-empty, and an
    existing destination is never silently clobbered.

    - Windows: `os.rename()` is atomic *and* raises `FileExistsError` when
      the destination exists — it never overwrites — so `tmp_path` is moved
      straight into place. On a LOST race (`FileExistsError`) `tmp_path`
      still exists on disk and must be removed explicitly here — unlike the
      won-race path, `os.rename()` does not consume it for us. Without this
      cleanup, `tmp_path` (a `.reserve-*.tmp` from `_reserve_placeholder` or
      a `.lock-*.tmp` from `_acquire_adopt_lock`) leaks forever: this is the
      live production path (the plugin is Windows-only), and `reap_orphans()`
      only globs `*.json`, never `*.tmp` (ticket #27 review finding 1).
    - POSIX: `os.rename()` would instead *silently overwrite* an existing
      destination (no exception, no signal), which would blind-clobber a
      live owner's slot/lock file — exactly the bug class finding 2 closed.
      `os.link()` gives the same "fails if destination exists" semantics as
      `O_CREAT | O_EXCL` (it atomically adds a second directory entry for
      the same inode, or raises `FileExistsError`), so it is used instead;
      `tmp_path` is then always removed since `dest` is now the file's
      canonical name (or, on failure, `tmp_path` is simply no longer needed).
    """
    if os.name == "nt":
        try:
            os.rename(tmp_path, dest)
            return True
        except FileExistsError:
            return False
        finally:
            # On the won-race path os.rename() already consumed tmp_path,
            # so this unlink is a harmless no-op (guarded below); on the
            # lost-race path it is the only thing that removes the temp
            # file at all.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    try:
        os.link(tmp_path, dest)
        published = True
    except FileExistsError:
        published = False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return published


def _reserve_placeholder(path: Path, session_id: str, owner_pid: int) -> bool:
    """Atomically create *path* with a placeholder SessionState.

    The complete placeholder JSON is written to a sibling temp file FIRST,
    then published to *path* via `_publish_if_absent()` — never
    os.open(O_CREAT|O_EXCL) directly on the final path followed by a
    separate os.write(). That two-step create-then-write left a window
    where a concurrent claimer could read *path* while it was still empty,
    hit JSONDecodeError, and take the "corrupt slot -> reclaim" branch in
    claim_session_slot(), overwriting this reservation (ticket #27 review
    finding 2). Returns True on success, False if *path* already existed.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".reserve-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.write(tmp_fd, _placeholder(session_id, owner_pid).to_json().encode("utf-8"))
    finally:
        os.close(tmp_fd)
    return _publish_if_absent(tmp_name, path)


def _lock_path(path: Path) -> Path:
    return path.parent / f"{path.name}.lock"


# How old an unparseable lock file must be before it's treated as breakable
# rather than as a live in-progress write (see _lock_is_unparseable_and_stale).
_LOCK_STALE_SECONDS = 30


def _lock_is_unparseable_and_stale(lock_path: Path) -> bool:
    """An unparseable lock file is only safe to break once it's old enough
    that no concurrent creator could plausibly still be mid-publish.

    With `_acquire_adopt_lock()` now publishing the lock's full PID content
    via `_publish_if_absent()` (content-first, then a single atomic
    rename/link — ticket #27 review finding 1), a lock file WE create can
    never be observed empty/unparseable: it either doesn't exist yet, or
    exists complete. This check exists purely as a self-healing backstop
    for a lock left over from before this fix, manual tampering, or a
    filesystem oddity — so such a lock is breakable (once clearly stale)
    rather than permanently treated as live, which would starve that slot
    forever (no code path ever revisits an unparseable lock otherwise).
    """
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        # Vanished already (e.g. the holder released it concurrently) —
        # nothing left to break.
        return True
    return age >= _LOCK_STALE_SECONDS


def _file_identity(path: Path) -> Optional[tuple]:
    """Return an identity token for *path*'s current on-disk content, or
    None if it cannot be read (already vanished, permission error, ...).

    The token pairs the file's content with filesystem-level identity
    (mtime, size, and inode/file-index when the platform exposes one) so a
    caller can later re-verify that the file it is about to unlink is still
    the *exact same file* it inspected earlier — see `_unlink_if_unchanged`.
    """
    try:
        content = path.read_bytes()
        st = path.stat()
    except OSError:
        return None
    return (content, st.st_mtime_ns, st.st_size, getattr(st, "st_ino", None))


def _unlink_if_unchanged(path: Path, expected_identity: tuple) -> bool:
    """Unlink *path*, but only if its identity still matches
    *expected_identity* captured earlier. Returns True once the file no
    longer exists at *path* under that identity (either it was just
    unlinked, or had already vanished); False if the identity no longer
    matches, in which case *path* is left completely untouched.

    This is the compare-and-swap that closes ticket #27 review finding 2:
    both call sites in `_acquire_adopt_lock()` used to call
    `lock_path.unlink()` unconditionally after merely inspecting the file's
    content, with no re-check immediately before the unlink. If a second
    process races in and republishes the lock (its own live pid) between
    that inspection and the unlink, a blind unlink would delete that OTHER
    process's live lock instead of the stale one actually inspected — and
    the breaker's subsequent retry would then succeed, letting it enter the
    critical section concurrently with the legitimate holder. Re-verifying
    identity right before the unlink call (rather than trusting the earlier
    read) closes that window down to the two syscalls straddling this
    function's own re-read and unlink, instead of spanning the caller's
    entire read-decide sequence (which also includes an `is_process_alive()`
    call that can take arbitrary time).

    Content bytes ALONE are not a sufficient identity check: a republished
    lock could in principle carry the same bytes as the one inspected (e.g.
    the exact same short numeric pid, coincidentally or via pid reuse).
    Pairing content with mtime/size/inode closes that gap here because
    every publish goes through `_publish_if_absent()`, which always creates
    a brand-new temp file via `tempfile.mkstemp()` and moves/links it into
    place — so a genuinely different publish event always gets a new
    inode and a new mtime, even when the pid string inside happens to
    match byte-for-byte.
    """
    if _file_identity(path) != expected_identity:
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


def _acquire_adopt_lock(path: Path) -> bool:
    """Try to become the sole adopter of the existing slot at *path*.

    Returns True when this process holds the lock and may safely read this
    slot's on-disk state, decide whether to adopt it, and write the result —
    all as one atomic-from-the-outside operation. Returns False when another
    process is concurrently deciding this same slot's fate (or holds a live
    lock); the caller must treat the slot as contended and move on to the
    next candidate slot rather than retrying, so two adopters never end up
    both winning the same dead/legacy owner (ticket #27 review finding 1).

    The lock file's content (the holder's pid) is written to a sibling temp
    file FIRST and only published to *lock_path* via `_publish_if_absent()`
    — never `os.open(O_CREAT|O_EXCL)` on the final path followed by a
    separate `os.write()`. That two-syscall gap meant a holder that died
    between the two calls (OOM kill, forced termination, power loss) left
    behind an empty, unparseable lock file that no code path ever revisited:
    every future caller hit the `except (OSError, ValueError): return False`
    branch forever, permanently starving that slot. With the content-first
    publish, a lock file we create is never observable half-written; an
    unparseable lock can now only be a pre-existing/foreign leftover, and
    `_lock_is_unparseable_and_stale()` lets even that self-heal once old.

    A lock file left behind by a process that has since died is broken and
    retried exactly once; the retry still goes through the same
    only-if-absent publish, so at most one of any number of concurrent
    breakers ever proceeds. Both break sites go through
    `_unlink_if_unchanged()`, a compare-and-swap that re-verifies the
    lock's identity immediately before unlinking it — so a breaker that
    inspected a since-superseded (stale) lock can never delete a
    DIFFERENT, freshly (and legitimately) republished live lock instead
    (ticket #27 review finding 2).
    """
    lock_path = _lock_path(path)
    me = os.getpid()
    for attempt in range(2):
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".lock-", suffix=".tmp", dir=str(lock_path.parent)
        )
        try:
            os.write(tmp_fd, str(me).encode("utf-8"))
        finally:
            os.close(tmp_fd)

        if _publish_if_absent(tmp_name, lock_path):
            return True

        if attempt == 1:
            return False

        # Capture the lock's identity (content + mtime/size/inode) in the
        # SAME read used to decide whether to break it, so any later break
        # attempt can re-verify — right before unlinking — that it is still
        # looking at this exact file and not one a concurrent process has
        # since republished (ticket #27 review finding 2). See
        # `_unlink_if_unchanged()` for why content alone isn't sufficient.
        identity = _file_identity(lock_path)
        if identity is None:
            # Vanished meanwhile (e.g. the holder released concurrently) —
            # nothing left to break; retry the publish.
            continue
        content, _mtime_ns, _size, _ino = identity
        try:
            holder_pid = int(content.decode("utf-8").strip())
        except (UnicodeDecodeError, ValueError):
            if not _lock_is_unparseable_and_stale(lock_path):
                # Fresh and unparseable — could be a foreign writer mid
                # publish. Treat conservatively as live.
                return False
            if not _unlink_if_unchanged(lock_path, identity):
                # The lock changed between our read and the unlink — a
                # concurrent process may have republished it live. Treat
                # conservatively as live rather than risk deleting a
                # different process's live lock.
                return False
            continue
        if is_process_alive(holder_pid):
            return False
        # Stale lock from a dead process — break it and retry once, but
        # only if it is still the exact file we just inspected.
        if not _unlink_if_unchanged(lock_path, identity):
            return False
        continue
    return False


def _release_adopt_lock(path: Path) -> None:
    try:
        _lock_path(path).unlink()
    except OSError:
        pass


def claim_session_slot(base_id: str) -> tuple[str, Optional[SessionState]]:
    """Return (session_id, reattachable_state) for this wrapper process.

    Resolves *base_id* (from resolve_session_id()) to a slot this process
    owns.  Two wrapper processes that compute the same base id — e.g. both
    landing on the deterministic host+user fallback — end up claiming
    distinct slots (``base_id``, ``base_id-2``, ``base_id-3``, ...) instead of
    silently reattaching to each other's Chrome instance.

    Returns
    -------
    tuple[str, Optional[SessionState]]
        ``session_id`` is the slot id this process now owns; the caller must
        use it for ``ChromeEngine.session_id`` and the eventual ``save_state``.
        ``reattachable_state`` is the ``SessionState`` the caller may reattach
        to (its Chrome ``pid``/``port`` are the candidates), or ``None`` when
        the slot was freshly reserved and the caller must launch Chrome.
    """
    me = os.getpid()

    for i in range(1, MAX_SESSION_SLOTS + 1):
        candidate = base_id if i == 1 else f"{base_id}-{i}"
        path = _state_dir() / f"{candidate}.json"

        if _reserve_placeholder(path, candidate, me):
            return candidate, None

        # Slot already exists — inspect who (if anyone) owns it. The whole
        # read + decide + write sequence below runs under an exclusive
        # adopt-lock: without it, two wrapper processes starting concurrently
        # right after a previous owner died could both read "owner dead /
        # Chrome alive", both decide to adopt, and both write themselves in
        # as owner_pid — landing on the SAME Chrome pid/port (ticket #27
        # review finding 1). The lock makes exactly one of them win; the
        # loser falls through to the next candidate slot instead of sharing.
        if not _acquire_adopt_lock(path):
            continue
        try:
            try:
                state = SessionState.from_json(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError, KeyError, OSError) as exc:
                logger.warning("Reclaiming unreadable/corrupt slot %s: %s", path, exc)
                save_state(_placeholder(candidate, me))
                return candidate, None

            if state.owner_pid is None:
                # Legacy JSON written before owner_pid existed.
                if is_process_alive(state.pid):
                    state.owner_pid = me
                    save_state(state)
                    return candidate, state
                save_state(_placeholder(candidate, me))
                return candidate, None

            if state.owner_pid == me:
                # This same wrapper re-initialising.
                return candidate, state

            if not is_process_alive(state.owner_pid):
                # Crash recovery: the previous wrapper is gone but its Chrome
                # may still be running — adopt the slot so the caller can try
                # to reattach (or fall through to a fresh launch if Chrome
                # died too).
                state.owner_pid = me
                save_state(state)
                return candidate, state

            # Slot held by a live foreign wrapper — try the next slot.
            continue
        finally:
            _release_adopt_lock(path)

    # All MAX_SESSION_SLOTS slots are held by live foreign owners.
    last_resort = f"{base_id}-{me}"
    logger.warning(
        "All %d session slots for base id %r are held by live wrappers; "
        "falling back to PID-suffixed slot %r",
        MAX_SESSION_SLOTS, base_id, last_resort,
    )
    path = _state_dir() / f"{last_resort}.json"
    if not _reserve_placeholder(path, last_resort, me):
        save_state(_placeholder(last_resort, me))
    return last_resort, None


def delete_state(session_id: str) -> None:
    """Remove the state JSON for *session_id* if it exists."""
    path = _state_dir() / f"{session_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
