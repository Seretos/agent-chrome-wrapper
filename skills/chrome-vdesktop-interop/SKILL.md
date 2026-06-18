---
name: chrome-vdesktop-interop
description: >
  Hand a Chrome window launched by agent-chrome-wrapper to agent-vdesktop for
  positioning, resizing, and virtual-desktop assignment.
  Use when you need to move, resize, or place a Chrome window on a virtual desktop.
  Trigger phrases: "move the Chrome window", "position Chrome on the desktop",
  "hand Chrome to vdesktop", "adopt the Chrome window", "resize Chrome",
  "place Chrome on virtual desktop".
---

# Chrome + vdesktop Interop

Hand a Chrome window launched by `agent-chrome-wrapper` to `agent-vdesktop` for
positioning, resizing, and virtual-desktop assignment.

## Recipe

```python
# 1. Get the Chrome window handle for this session
info = chrome_wrapper.get_instance_info()
hwnd = info["hwnd"]      # integer HWND; None if Chrome is not yet visible

# 2. Adopt the window in vdesktop (returns a handle_id for subsequent calls)
handle_id = vdesktop.adopt_window(hwnd=hwnd)

# 3. Position and resize as needed
vdesktop.move_window(handle_id=handle_id, x=0, y=0)
vdesktop.resize_window(handle_id=handle_id, width=1280, height=800)
```

## Notes

- `hwnd` is `None` while Chrome is still starting. Call `get_instance_info()`
  again after a short wait if `hwnd` is `None`.
- `adopt_window` accepts a raw HWND integer directly — no session ID translation needed.
- All three calls use the same MCP session; no shared state exists between plugins.
