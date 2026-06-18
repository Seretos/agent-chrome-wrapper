# agent-chrome-wrapper

Provides an MCP that gives agents access to Chrome, starting a separate Chrome instance for each session.

## Quick install

**Claude Code:**

```
/plugin marketplace add Seretos/agent-marketplace
/plugin install agent-chrome-wrapper@agent-marketplace
```

Self-contained binary — no Python, no `pip install`, no dependencies. This is a Windows-only plugin; the release zip ships the native Windows binary (`chrome-wrapper.exe`).

## Alternative installs

### From the GitHub Releases page

1. Download `agent-chrome-wrapper-<version>.zip` from [Releases](https://github.com/Seretos/agent-chrome-wrapper/releases).
2. Unpack to a stable folder (e.g. `C:\Users\<you>\.claude\plugins\agent-chrome-wrapper\`).
3. In Claude Code:
   ```
   /plugin install <path-to-unpacked-folder>
   ```

### From the release branch

The `release` branch always carries the latest install-ready files (no zip step):

```
git clone --branch release --depth 1 https://github.com/Seretos/agent-chrome-wrapper.git
```

Then `/plugin install <cloned-path>` in Claude Code.

### Build from source

Requires Python 3.11+ (standard python.org installer with the `py` launcher on Windows).

```powershell
git clone https://github.com/Seretos/agent-chrome-wrapper.git
cd agent-chrome-wrapper
pwsh -File scripts/build.ps1 -Clean -Package
```

Output: `bin/chrome-wrapper.exe`. Then install via `/plugin install <path>`.
