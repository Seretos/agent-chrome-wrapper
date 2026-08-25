# PyInstaller spec for the agent-chrome-wrapper MCP server.
#
# Produces a single-file self-contained binary that bundles the Python
# interpreter, the MCP runtime, and the package itself. Output extension is
# host-OS-dependent — `.exe` on Windows, no extension on Linux. PyInstaller
# handles the per-OS suffix automatically; this spec is OS-agnostic.
#
# Build:    pwsh -File scripts/build.ps1 -Clean
# Output:   dist/chrome-wrapper.exe on Windows, dist/chrome-wrapper on Linux
# Copy to:  bin/chrome-wrapper(.exe)  (handled by scripts/build.ps1)

# ruff: noqa
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None
ROOT = Path(SPECPATH)

# `mcp.cli` requires optional `typer`/`rich` deps the server doesn't need.
# Collect mcp manually, filtering out the CLI subpackage so PyInstaller doesn't
# fail trying to import it.
def _not_cli(name: str) -> bool:
    return not name.startswith("mcp.cli")

mcp_hiddenimports = collect_submodules("mcp", filter=_not_cli)

# `fastmcp.cli` requires optional `typer`/`rich`-adjacent deps the server
# doesn't need (same reasoning as `mcp.cli` above). Collect fastmcp manually,
# filtering out the CLI subpackage.
def _not_fastmcp_cli(name: str) -> bool:
    return not name.startswith("fastmcp.cli")

fastmcp_hiddenimports = collect_submodules("fastmcp", filter=_not_fastmcp_cli)
# fastmcp/__init__.py calls importlib.metadata.version("fastmcp") at import
# time (for __version__); the frozen build has no dist-info on sys.path
# unless we copy it in explicitly, which raises PackageNotFoundError and
# crashes the server before it can even start (caught by the smoke test).
fastmcp_datas = collect_data_files("fastmcp") + copy_metadata("fastmcp")

# fastmcp's server startup unconditionally spins up a `docket` in-memory task
# queue as part of its own internal lifespan (independent of the `lifespan=`
# we pass to FastMCP()); docket._redis lazily resolves its in-memory backend
# via `importlib.import_module("burner_redis")` — a dynamic string import
# PyInstaller's static analysis can't see, so it's missed unless collected
# explicitly. burner_redis also ships a native `_burner_redis.pyd` extension,
# so collect_all() (not collect_submodules()) is needed to pull in the binary.
from PyInstaller.utils.hooks import collect_all as _collect_all_early
_br_datas, _br_bins, _br_hidden = _collect_all_early("burner_redis")

extra_hidden = [
    # fastmcp 2.x runtime (standalone package, ticket #26):
    "anyio",
    "pydantic",
    "pydantic_core",
    "starlette",
]
extra_hidden += collect_submodules("chrome_wrapper_plugin")
extra_hidden += collect_submodules("websocket")

# pywin32 ships native extension DLLs that PyInstaller misses without
# explicit collection. collect_all("pywin32") is the preferred single-call
# form; fall back to collect_all("win32api") which carries the shared DLL
# set used by win32gui/win32process/win32con.
import sys as _sys
if _sys.platform == "win32":
    from PyInstaller.utils.hooks import collect_all as _collect_all
    try:
        _pw32_datas, _pw32_bins, _pw32_hidden = _collect_all("pywin32")
    except Exception:
        _pw32_datas, _pw32_bins, _pw32_hidden = _collect_all("win32api")
    extra_hidden += _pw32_hidden
else:
    _pw32_bins, _pw32_datas = [], []

a = Analysis(
    ["src/chrome_wrapper_plugin/__main__.py"],
    pathex=[str(ROOT / "src")],
    binaries=_pw32_bins + _br_bins,
    datas=_pw32_datas + fastmcp_datas + _br_datas,
    hiddenimports=mcp_hiddenimports + fastmcp_hiddenimports + extra_hidden + _br_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "PIL",
        "test",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="chrome-wrapper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # don't compress — slower startup, no real size win on stdio binaries
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # MUST be console=True for stdio MCP transport
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
