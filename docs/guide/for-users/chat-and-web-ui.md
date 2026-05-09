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

## A2A endpoint (advanced)

The web server also exposes an [A2A](../../concepts/a2a.md) JSON-RPC endpoint for programmatic access and agent-to-agent communication:

```
POST http://localhost:8080/a2a/agents/<agent-name>
```

This is useful for scripting, CI pipelines, or connecting Reyn to another agent system.
See [concepts/a2a](../../concepts/a2a.md) for the protocol details.

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
- [Concepts: A2A](../../concepts/a2a.md) — agent-to-agent protocol
