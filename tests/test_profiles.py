"""Tests for chrome_wrapper_plugin.profiles.

All tests are filesystem-only (tmp_path); no real Chrome is launched.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from chrome_wrapper_plugin.profiles import (
    master_profile_dir,
    promote,
    seed_profile,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_master(base: Path) -> Path:
    """Create a minimal master-profile tree for testing."""
    master = base / "master"
    master.mkdir()
    # Files
    (master / "Local State").write_text('{"local": "state"}')
    (master / "Default").mkdir()
    (master / "Default" / "Cookies").write_text("cookies")
    (master / "Default" / "Login Data").write_text("logins")
    (master / "Default" / "Preferences").write_text('{}')
    # Network sub-dir
    (master / "Default" / "Network").mkdir()
    (master / "Default" / "Network" / "Cookies").write_text("net-cookies")
    # Local Storage dir
    ls = master / "Default" / "Local Storage"
    ls.mkdir()
    (ls / "leveldb").write_text("db")
    (ls / "SingletonLock").write_text("lock")
    # IndexedDB dir
    idb = master / "Default" / "IndexedDB"
    idb.mkdir()
    (idb / "data.db").write_text("idb")
    return master


# ── seed_profile ─────────────────────────────────────────────────────────────

def test_seed_profile_copies_files(tmp_path):
    master = _make_master(tmp_path)
    session = tmp_path / "session"
    session.mkdir()

    seed_profile(master, session)

    assert (session / "Local State").read_text() == '{"local": "state"}'
    assert (session / "Default" / "Cookies").read_text() == "cookies"
    assert (session / "Default" / "Login Data").read_text() == "logins"
    assert (session / "Default" / "Network" / "Cookies").read_text() == "net-cookies"
    assert (session / "Default" / "Preferences").read_text() == "{}"


def test_seed_profile_copies_dirs(tmp_path):
    master = _make_master(tmp_path)
    session = tmp_path / "session"
    session.mkdir()

    seed_profile(master, session)

    assert (session / "Default" / "Local Storage" / "leveldb").exists()
    assert (session / "Default" / "IndexedDB" / "data.db").exists()


def test_seed_profile_skips_singleton_lock(tmp_path):
    master = _make_master(tmp_path)
    session = tmp_path / "session"
    session.mkdir()

    seed_profile(master, session)

    # SingletonLock must NOT be copied into the session
    assert not (session / "Default" / "Local Storage" / "SingletonLock").exists()


def test_seed_profile_skips_missing_items(tmp_path):
    """Items absent in master are skipped without raising."""
    master = tmp_path / "master"
    master.mkdir()
    # Only "Local State" — everything else is missing
    (master / "Local State").write_text("x")

    session = tmp_path / "session"
    session.mkdir()

    seed_profile(master, session)  # must not raise

    assert (session / "Local State").exists()
    # Other items simply absent
    assert not (session / "Default" / "Cookies").exists()


def test_seed_profile_noop_if_master_absent(tmp_path):
    """seed_profile returns silently when master directory does not exist."""
    master = tmp_path / "nonexistent_master"
    session = tmp_path / "session"
    session.mkdir()

    seed_profile(master, session)  # must not raise

    # Session dir should be empty (nothing was copied)
    assert list(session.iterdir()) == []


# ── promote ───────────────────────────────────────────────────────────────────

def test_promote_copies_subset_back(tmp_path):
    master = _make_master(tmp_path)
    session = tmp_path / "session"
    session.mkdir()
    seed_profile(master, session)

    # Modify something in the session
    (session / "Local State").write_text('{"local": "updated"}')

    promote(session, master)

    assert (master / "Local State").read_text() == '{"local": "updated"}'


# ── master_profile_dir ────────────────────────────────────────────────────────

def test_master_profile_dir_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "my_profile"
    monkeypatch.setenv("CHROME_WRAPPER_MASTER_PROFILE", str(custom))
    assert master_profile_dir() == custom


def test_master_profile_dir_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROME_WRAPPER_MASTER_PROFILE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    result = master_profile_dir()
    assert result == tmp_path / "ChromeWrapperPlugin" / "MasterProfile"
