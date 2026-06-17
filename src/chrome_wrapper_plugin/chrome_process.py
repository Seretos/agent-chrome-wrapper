"""Chrome binary discovery, launch, CDP readiness polling, and teardown.

Pure stdlib — no external dependencies beyond what is already declared.
"""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows creation flag: spawn Chrome in its own process group so it is not
# killed when the MCP server's console closes.
_CREATE_NEW_PROCESS_GROUP = 0x00000200


# ── Chrome binary discovery ─────────────────────────────────────────────────

def find_chrome() -> Path:
    """Return the path to the Chrome executable.

    Probe order:
    1. ``CHROME_WRAPPER_CHROME_PATH`` environment variable (override).
    2. ``HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe``.
    3. ``HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe``.
    4. ``%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe``.
    5. ``%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe``.
    6. ``%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe``.

    Raises ``FileNotFoundError`` listing all probed paths when nothing resolves.
    """
    probed: list[str] = []

    # 1. Environment-variable override
    env_path = os.environ.get("CHROME_WRAPPER_CHROME_PATH", "").strip()
    if env_path:
        p = Path(env_path)
        probed.append(str(p))
        if p.is_file():
            return p
        # Env was set but path doesn't exist — still fail normally so the
        # caller gets a clear error.

    # 2–3. Windows registry (App Paths)
    try:
        import winreg  # type: ignore[import-not-found]

        _REG_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, _REG_KEY) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        p = Path(value)
                        probed.append(str(p))
                        if p.is_file():
                            return p
            except OSError:
                # Key absent on this hive — try next
                pass
    except ImportError:
        # Not on Windows (e.g. CI running on Linux); skip registry entirely
        pass

    # 4–6. Filesystem fallbacks
    rel = Path("Google") / "Chrome" / "Application" / "chrome.exe"
    dirs: list[str | None] = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LocalAppData"),
    ]
    for base_str in dirs:
        if not base_str:
            continue
        p = Path(base_str) / rel
        probed.append(str(p))
        if p.is_file():
            return p

    raise FileNotFoundError(
        "Chrome executable not found. Probed paths:\n"
        + "\n".join(f"  {path}" for path in probed)
        + "\nSet CHROME_WRAPPER_CHROME_PATH to override."
    )


# ── Port allocation ──────────────────────────────────────────────────────────

def find_free_port() -> int:
    """Bind an ephemeral TCP port on 127.0.0.1, release it, and return the number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Launch ───────────────────────────────────────────────────────────────────

def launch_chrome(user_data_dir: Path, port: int) -> subprocess.Popen:
    """Launch a headful Chrome instance with a dedicated CDP port.

    The process is detached from the server's stdio and placed in its own
    process group so that a console close does not cascade to Chrome.
    """
    chrome_path = find_chrome()
    args = [
        str(chrome_path),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NEW_PROCESS_GROUP,
    )


# ── CDP readiness ────────────────────────────────────────────────────────────

def wait_for_cdp(port: int, timeout: float = 30.0) -> None:
    """Poll ``/json/version`` until Chrome's DevTools endpoint responds.

    Raises ``TimeoutError`` when *timeout* seconds elapse without a successful
    response.  Uses plain HTTP only — WebSocket (CDP sessions) is out of scope
    for this ticket.
    """
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, http.client.RemoteDisconnected, OSError):
            pass
        time.sleep(0.25)

    raise TimeoutError(
        f"Chrome CDP endpoint on port {port} did not respond within {timeout}s."
    )


# ── Teardown ─────────────────────────────────────────────────────────────────

def terminate_chrome(
    proc: subprocess.Popen | None, user_data_dir: Path
) -> None:
    """Terminate *proc* (if given) and delete *user_data_dir*.

    Safe to call when *proc* has already exited or is ``None`` (reattach
    case where we hold no Popen handle).
    """
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            # Process may have already exited — ignore
            pass

    shutil.rmtree(user_data_dir, ignore_errors=True)
