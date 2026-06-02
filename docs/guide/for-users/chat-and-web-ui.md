---
type: how-to
topic: web-ui
audience: [human]
---

# Chat and Web UI

Reyn has two interfaces: the **TUI** (terminal) and the **Web UI** (browser). They connect to the same agent and share the same session — you can switch between them freely.

---

## Start the TUI

```bash
reyn chat
```

The TUI gives you a `>` prompt. Type requests, see responses inline. It's the fastest way to start.

---

## Start the Web UI

In a second terminal:

```bash
reyn web
```

Then open **http://localhost:8080** in any browser.

The Web UI shows conversation history, running skill status, and richer output rendering (tables, code blocks, markdown). Use it when you want a more readable view or when sharing a screen.

### Custom host and port

```bash
reyn web --port 9000            # change port
reyn web --host 0.0.0.0         # accept connections from other machines (LAN)
```

> **Security note**: The default `127.0.0.1` binding accepts connections from localhost only. Use `--host 0.0.0.0` only on trusted networks.

---

## TUI and Web UI side by side

Both interfaces talk to the same agent. Starting a task in the TUI and then watching it in the browser works — both views update as the skill runs.

```
Terminal A          Terminal B          Browser
──────────────      ──────────────      ──────────────────
$ reyn chat         $ reyn web          http://localhost:8080
> write a report    (serving...)        [live progress view]
```

---

## Stopping

- **TUI**: `Ctrl+D` or `/quit`
- **Web server**: `Ctrl+C` in the terminal where `reyn web` is running

The two processes are independent. Stopping one does not affect the other.

---

## TUI keyboard shortcuts

The TUI footer shows a five-key strip — handy at a glance but not exhaustive.
Press `Ctrl+B` to open the right panel and switch to the **Keys** tab for the
full live list (the tab reflects the actual bindings the app is loaded with,
including any voice-mode keys when recording).

The shortcuts you reach for daily:

### Input

| Key | Action |
|-----|--------|
| `Enter` | Send the current prompt |
| `Ctrl+J` | Insert a newline (paste a multi-line prompt) |
| `Ctrl+U` | Clear the input buffer (single- or multi-line) |
| `↑` / `↓` | Walk through past prompts (when the slash picker is closed) |
| `Tab` | Confirm-without-send when the slash picker is open |
| `Esc` | Dismiss the slash picker / docs filter / pending hint |

### Conversation

| Key | Action |
|-----|--------|
| `Ctrl+P` / `Ctrl+N` | Jump to the previous / next turn header |
| `Ctrl+L` | Clear the conversation pane (engine state untouched) |
| `Ctrl+C` | Cancel the in-flight skill / LLM call / intervention modal |

### Right panel

| Key | Action |
|-----|--------|
| `Ctrl+B` | Toggle the right panel |
| `Ctrl+W` | Cycle to the next tab (Keys → Events → Agents → Memory → Cost → Docs → Pending) |
| `h` / `l` | Widen / narrow the panel |
| `j` / `k` | Scroll the current tab |
| `space` | Toggle the preview pane for the cursor row (events / agents / memory / docs / pending only) |
| `c` | Copy the current view; on the Pending tab, claim the cursor's intervention |

### Quit

| Key | Action |
|-----|--------|
| `Ctrl+D` | Quit the TUI (also `/quit`) |

> Slash commands like `/copy`, `/cancel`, `/list`, `/skill`, `/plan`,
> `/agents`, `/attach`, `/tasks` are documented in the
> [`reyn chat` reference](../../reference/cli/chat.md#slash-commands).

---

## A2A endpoint (advanced)

The web server also exposes an [A2A](../../concepts/multi-agent/a2a.md) JSON-RPC endpoint for programmatic access and agent-to-agent communication:

```
POST http://localhost:8080/a2a/agents/<agent-name>
```

This is useful for scripting, CI pipelines, or connecting Reyn to another agent system.
See [concepts/a2a](../../concepts/multi-agent/a2a.md) for the protocol details.

---

## Troubleshooting

**Port already in use**

```
ERROR: [Errno 48] Address already in use
```

Another process is on port 8080. Use `--port` to pick a different one:

```bash
reyn web --port 8081
```

**Can't connect from another device**

By default the server binds to `127.0.0.1` (localhost only). Run with `--host 0.0.0.0` to accept LAN connections.

---

## See also

- [Reference: CLI / chat](../../reference/cli/chat.md) — TUI slash commands
- [Concepts: A2A](../../concepts/multi-agent/a2a.md) — agent-to-agent protocol
