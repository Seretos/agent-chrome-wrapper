# agent-chrome-wrapper

PyInstaller-frozen Python MCP server, shipped as a self-contained Windows binary (`bin/chrome-wrapper.exe`). End users need no Python toolchain. The MCP starts a separate Chrome instance per session, and is intended to pair with `agent-vdesktop` (launch Chrome here, position/manage the window there) on a shared Windows host.

## Contracts an agent won't infer from the tree

- **Windows-only plugin.** `OS_TARGETS = [windows]`: `release.yml`'s build matrix and `test.yml` run Windows only, and both manifests' `command` carries an explicit `.exe` (`bin/chrome-wrapper.exe`). Under Linux the plugin is expected to run against a Chrome on the host. Don't re-add the Linux matrix row unless the plugin gains a real cross-platform binary path.
- **Release is orphan-branch + marketplace dispatch.** `release.yml` (manual: Actions → release → `version=X.Y.Z`) stamps the version, builds the Windows binary, then force-pushes an orphan `release` branch holding only install-ready files and POSTs a dispatch to `Seretos/agent-marketplace`. `main` and `release` share no history — never merge between them. Clients install at the tag `agent-chrome-wrapper--vX.Y.Z`.
- **Version is pipeline-owned.** The `version` in `pyproject.toml` and both manifests is a placeholder; the workflow input is the source of truth and the stamp never lands on `main`. Don't hand-bump it.
- **Two host manifests, no `.mcp.json`.** `.claude-plugin/plugin.json` resolves its `command` via `${CLAUDE_PLUGIN_ROOT}`; `.codex-plugin/plugin.json` via `${PLUGIN_ROOT}`. Both carry an inline `mcpServers` block because neither placeholder expands in the other host. Keep the two in sync.
- **Required secret:** `MARKETPLACE_DISPATCH_TOKEN` — fine-grained PAT, `Contents: RW` + `Pull requests: RW` on `Seretos/agent-marketplace` only.
- **`assets/icon.png` is a release artifact, not just a repo file.** The dispatch payload sends a `raw.githubusercontent.com/${repo}/${TAG}/assets/icon.png` URL to the marketplace, so the file must live on the orphan `release` branch at the tagged commit — `release.yml` copies `stamped/assets/` into the staging tree for exactly that reason. Ship `assets/icon.png` from day one or the marketplace listing has no image.
- **`description.md` is a release artifact, not just a repo file.** The dispatch payload sends a `raw.githubusercontent.com/${repo}/${TAG}/description.md` URL in the `description_url` field, so the file must live on the orphan `release` branch at the tagged commit — `release.yml` copies it into the staging tree alongside `assets/`. Fill in its Key Features before cutting v0.0.1.

## Gotchas (the "why" behind the code)

- **`build.ps1` runs under Windows PowerShell 5.1, PS7, and Linux `pwsh`.** It derives `$IsWindows` from `$env:OS` (5.1 lacks the auto variable) and sets no global `$ErrorActionPreference='Stop'` (PyInstaller floods stderr, which 5.1 wraps as ErrorRecords and would trip a global Stop). The smoke step gates the build on a real MCP `initialize` handshake. The script still branches on `$IsWindows` for the (unused on this plugin) Linux path — that's intentional, leave it.
- **Native bindings need `collect_all(...)` in `chrome-wrapper.spec`** — PyInstaller misses their lazily-generated submodules otherwise. If you add a Chrome-driver library (Selenium, Playwright, pychrome, CDP client) that ships native bits, wire it in there.
