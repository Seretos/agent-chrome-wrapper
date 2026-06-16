"""HWND resolution for Chrome top-level browser-frame windows.

Windows-only: requires pywin32 (win32gui, win32process).
Returns (None, None) on non-Windows or when pywin32 is not installed.
"""
from __future__ import annotations


def find_chrome_hwnd(pid: int) -> tuple[int | None, str | None]:
    """Return (hwnd, window_title) for Chrome's top-level browser frame owned by *pid*.

    Matches the first visible top-level window with:
    - window PID == pid
    - class name == "Chrome_WidgetWin_1"
    - IsWindowVisible == True
    - non-empty title

    Returns (None, None) on non-Windows, when pywin32 is absent, or when no
    matching window is found.
    """
    try:
        import win32gui
        import win32process
    except ImportError:
        return None, None

    result: list[tuple[int, str]] = []

    def _callback(hwnd: int, _lparam: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if win32gui.GetClassName(hwnd) != "Chrome_WidgetWin_1":
            return True
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid == pid:
            result.append((hwnd, title))
            return False  # stop enumeration — first match wins
        return True

    win32gui.EnumWindows(_callback, None)
    if result:
        return result[0]
    return None, None
