# agent-chrome-wrapper

Provides an MCP that gives agents access to Chrome, starting a separate Chrome instance for each session.

## Key features

- **Per-session isolated Chrome** — each agent session gets its own dedicated Chrome instance with a separate user-data directory; sessions never interfere with each other.
- **Full browser tooling over CDP** — navigate pages, capture screenshots, evaluate JavaScript, query page info, and send any raw CDP command via the escape-hatch `cdp()` tool.
- **Pairs with agent-vdesktop** — `get_instance_info` surfaces the Chrome window's HWND so `agent-vdesktop` can adopt, position, and resize the browser window on the same Windows host.
