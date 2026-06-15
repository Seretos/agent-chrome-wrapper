# Security Policy

## Threat model

`chrome_wrapper_plugin` is a **local** MCP server. It runs as a process launched
by an MCP client (typically Claude Code) on the same machine as the user,
with the user's own privileges. It does not listen on a network socket and
is not designed to be exposed beyond the host.

The trust boundary is the MCP client: anything that can reach the server's
stdio already runs as the user. The tools exposed here are accordingly
authority-equivalent to "the user runs commands themselves" — within the
scope of whatever credentials or filesystem permissions the user has.

## Out of scope

- Compromise of the host machine where the plugin runs (the user already
  owns it).
- Misuse of the plugin's tools by a malicious local MCP client — that client
  already runs as the user.

## Reporting a vulnerability

For unexpected authority escalation, input validation gaps that escape the
documented contract of a tool, or any other security concern, open a GitHub
issue with the label `security` (or a private security advisory if the
repository supports them).

---

<!--
EXTEND THIS FILE with plugin-specific sections as the surface area grows.
Sections worth adding for this plugin given its tool surface (it launches
and drives real Chrome instances per session):

  ## Browser launch & navigation
  Document the contract for any tool that launches Chrome or navigates to a
  URL. A spawned Chrome runs with the user's privileges and can reach the
  user's own cookies/profile if pointed at a real profile dir — state
  whether each session gets an isolated, throwaway profile/user-data-dir or
  shares the user's. State whether arbitrary URLs / file:// / chrome:// are
  allowed and what (if anything) is blocked.

  ## Per-session instance lifecycle
  This MCP starts a dedicated Chrome instance per session. Document how
  instances are tracked, when they're torn down, and what happens to orphaned
  Chrome processes if a session dies abnormally (resource exhaustion is a DoS
  vector on the host).

  ## Remote debugging port / CDP surface
  If sessions expose a Chrome DevTools Protocol endpoint (e.g.
  --remote-debugging-port), that port is an authority-equivalent control
  channel for the browser. Document whether it binds to loopback only, whether
  the port is randomized per session, and whether any token gates it.

  ## Intentional shell execution
  If any tool forwards a string into a shell or into Chrome's command line
  (flags, profile paths, etc.), document which fields are executed/forwarded
  by design vs. which are constrained to a safe charset.
  Pattern: see agent-vdesktop/SECURITY.md ("Intentional shell execution").
-->
