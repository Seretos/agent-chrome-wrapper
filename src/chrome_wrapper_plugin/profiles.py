"""Chrome profile management: master-profile seeding and promotion.

Architecture invariant: Chrome refuses to share a user-data-dir across
processes (SingletonLock).  We keep a single persistent *master* profile
(logged in once) and *copy* it into a fresh per-session directory at launch.
Session writes are ephemeral; an explicit ``promote()`` call writes them back
to the master — callers must serialise concurrent promotes to avoid corruption.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from chrome_wrapper_plugin.chrome_process import find_chrome

logger = logging.getLogger(__name__)

# Subset of profile entries that carry login state.
# Tuples: (relative path inside user-data-dir, is_directory)
_PROFILE_SUBSET: list[tuple[str, bool]] = [
    ("Local State", False),
    ("Default/Cookies", False),
    ("Default/Network/Cookies", False),
    ("Default/Login Data", False),
    ("Default/Preferences", False),
    ("Default/Local Storage", True),
    ("Default/IndexedDB", True),
]


def master_profile_dir() -> Path:
    """Return the path to the persistent master profile directory.

    Honours ``CHROME_WRAPPER_MASTER_PROFILE`` env-var override; otherwise
    uses ``%LocalAppData%\\ChromeWrapperPlugin\\MasterProfile``.
    """
    override = os.environ.get("CHROME_WRAPPER_MASTER_PROFILE", "").strip()
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        return Path(local_app_data) / "ChromeWrapperPlugin" / "MasterProfile"
    # Last-resort fallback (e.g. non-Windows CI)
    return Path.home() / ".chrome_wrapper_plugin" / "MasterProfile"


def _copy_item(src: Path, dst: Path, is_dir: bool) -> None:
    """Copy a single profile item from *src* to *dst*, creating parents."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_dir:
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(
                src,
                dst,
                ignore=shutil.ignore_patterns("SingletonLock"),
            )
    else:
        if src.is_file():
            shutil.copy2(src, dst)


def seed_profile(master: Path, session_dir: Path) -> None:
    """Copy the login-state subset from *master* into *session_dir*.

    Items missing in *master* or locked (PermissionError) are skipped silently
    at DEBUG level — Chrome will recreate them on first run.
    """
    if not master.exists():
        logger.debug("Master profile %s absent — starting with clean profile.", master)
        return

    for rel, is_dir in _PROFILE_SUBSET:
        src = master / rel
        dst = session_dir / rel
        try:
            _copy_item(src, dst, is_dir)
        except FileNotFoundError:
            logger.debug("Profile item %s not found in master — skipping.", rel)
        except PermissionError as exc:
            logger.debug("Profile item %s locked — skipping: %s", rel, exc)


def promote(session_dir: Path, master: Path) -> None:
    """Copy the login-state subset from *session_dir* back into *master*.

    CALLERS MUST SERIALISE: concurrent calls corrupt the master profile.
    This function is never called automatically — it must be triggered
    explicitly by the agent when it wants to persist session state.
    """
    master.mkdir(parents=True, exist_ok=True)
    for rel, is_dir in _PROFILE_SUBSET:
        src = session_dir / rel
        dst = master / rel
        try:
            _copy_item(src, dst, is_dir)
        except FileNotFoundError:
            logger.debug("Session item %s not found — skipping promote.", rel)
        except PermissionError as exc:
            logger.debug("Session item %s locked during promote — skipping: %s", rel, exc)


def launch_for_initial_login() -> subprocess.Popen:
    """Open Chrome against the master profile for initial login setup.

    No CDP port is used and no seeding is performed — this is meant for a
    human operator to log in interactively.

    WARNING: No other Chrome instances must be running against the master
    profile directory while this process is active.
    """
    chrome_path = find_chrome()
    master = master_profile_dir()
    master.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            str(chrome_path),
            f"--user-data-dir={master}",
            "--no-first-run",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
