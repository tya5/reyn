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

The Web UI shows conversation history, running workflow status, and richer output rendering (tables, code blocks, markdown). Use it when you want a more readable view or when sharing a screen.

### Custom host and port

```bash
reyn web --port 9000            # change port
reyn web --host 0.0.0.0         # accept connections from other machines (LAN)
```

> **Security note**: The default `127.0.0.1` binding accepts connections from localhost only. A non-loopback bind (like `--host 0.0.0.0`) refuses to start unless a bearer token is configured (`web.auth.token` in `reyn.yaml`) — the gateway never exposes itself to the network unauthenticated.

---

## Remote thin client (`reyn chat --connect`)

`reyn chat --connect` attaches a thin terminal client to a `reyn web` server someone else is running (or one you started earlier) — the server holds the session; your terminal just streams the conversation and relays your input.

### Start the server

```bash
reyn web --host 0.0.0.0 --port 8080
```

On the default loopback bind (`127.0.0.1`), reyn generates a launch token and prints it in the startup URL (`http://127.0.0.1:8080/?token=...`). On a non-loopback bind, reyn refuses to start unless you've configured `web.auth.token` in `reyn.yaml` — copy that token (or the printed URL) before connecting from elsewhere.

### Connect

```bash
pip install reyn[web]   # once, for the httpx client dependency
reyn chat --connect http://<host>:8080 --token <secret> [agent_name]
```

- `agent_name` is optional and picks which agent on the server you attach to (same as local `reyn chat <agent_name>`).
- `--token` can be omitted if `REYN_WEB_AUTH_TOKEN` is already set in your environment.
- The connection is plain HTTP + Server-Sent Events (AG-UI) — there's nothing else to open or forward besides the one port.

### What you get

Replies, tool activity, and status stream in as they happen on the server. A human-in-the-loop prompt (a permission ask, a clarifying question) can be answered from the remote terminal exactly like a local one — your answer is delivered to the server by id, so it lands correctly even with other clients attached to the same agent.

**Multiple clients see each other's turns.** If two or more clients (a local `reyn chat` and one or more `--connect` terminals, or several `--connect` terminals) attach to the same agent, everyone sees the full conversation — not just the agent's replies. Whoever types a message or answers a human-in-the-loop prompt, every OTHER attached client sees that line too, tagged with who sent it (e.g. `user [alice]:`) when more than one identity is attached; a single attached client shows the plain line with no tag.

### What's different from local `reyn chat`

- **Same inline CUI, streamed status bar.** On an interactive TTY, `--connect` renders the same inline CUI as local `reyn chat`, including the main status bar — `model` / `agent` / `cost` / `ctx%` chips and the working indicator — with those values streamed live from the server. (`--cui`, a non-TTY, or piped output still falls back to the plain console style, exactly like local.)
- **Status-bar *dropdowns*, the `task` chip, and pickers are local-only.** The streamed chip *values* render, but opening a chip's dropdown (the cost/context detail, the `/model` class picker, the agent / task tree, the `…` overflow toggles) shows an empty panel on a remote attach — that detail is session-local and not on the wire. The `task` chip shows `0` (the task count is not streamed). A closed-set human-in-the-loop prompt (a permission `[y]es` / `[n]o`) is answered by **typing** the choice on the input line rather than through the ↑↓ region picker.
- **`/rewind` is a text list, not the picker.** Locally `/rewind` opens an interactive ↑↓ region picker; over `--connect` it prints the same checkpoints as a plain numbered list instead.
- **No local file access.** `--connect` is a pure transport client — it never touches a local session, workspace, or tool. Everything runs on the server's machine.

### Security notes

- For same-machine thin-client use, prefer a UNIX domain socket instead of a token: `reyn web --uds /path/to/socket` — the connection is authenticated by OS peer credentials, no token needed.
- Any network bind (anything other than loopback) always requires `web.auth.token` and runs over TLS (self-signed by default; reyn prints the certificate fingerprint to pin on first connect).
- Treat the printed token/URL like a password — anyone who has it can act as the operator.

---

## TUI and Web UI side by side

Both interfaces talk to the same agent. Starting a task in the TUI and then watching it in the browser works — both views update as the workflow runs.

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

The default interactive `reyn chat` (any TTY) is an inline CUI — the conversation
prints into your terminal's own scrollback, with a status bar and an input box
below it. There's no separate panel to toggle; the status bar's chips (`model`,
`agent`, `task`, `cost`, `ctx`, and a `more` chip for `tool`/`mcp`/`skill`/`pipe`/
`hook`/`cron`) are always visible and drill down in place.

### Input

| Key | Action |
|-----|--------|
| `Enter` | Send the current prompt (or insert a newline, if your terminal sends Shift+Enter as a distinguishable escape) |
| `Ctrl+J` | Insert a newline — the guaranteed-works fallback on any terminal, for pasting or writing a multi-line prompt |
| `↑` | Walk back through prompt history (from an empty input, or the first line of one) |
| `↓` | Walk forward through history, move the cursor down inside a multi-line prompt, or (from an empty input) move focus down to the status bar |
| `Tab` / `Enter` | Accept the highlighted entry when the `/` slash-command completion menu is open |

### Status bar

Press `↓` from an empty input to focus the status bar, then:

| Key | Action |
|-----|--------|
| `←` / `→` | Move between chips (or between sub-bar chips, once `more` is open) |
| `Enter` | Open the focused chip's detail view (or, for an actionable one like `model`, apply the selected row) |
| `↑` / `↓` | Navigate rows inside an open detail view; at the top, `↑` closes it and returns focus to the input |
| `Esc` | Close the open detail view / sub-bar |

### Turn control

| Key | Action |
|-----|--------|
| `Ctrl+C` | Cancel the in-flight turn (a second `Ctrl+C` quits) |
| `Ctrl+D` / `Ctrl+Q` | Quit (also `/quit`) |

> Slash commands are documented in the
> [`reyn chat` reference](../../reference/cli/chat.md#slash-commands).

---

## A2A endpoint (advanced)

The web server also exposes an [A2A](../../concepts/multi-agent/a2a.md) JSON-RPC endpoint for programmatic access and agent-to-agent communication:

```
POST http://localhost:8080/a2a/agents/<agent-name>
```

This is useful for scripting, CI pipelines, or connecting Reyn to another agent system.
See [concepts/a2a](../../concepts/multi-agent/a2a.md) for the protocol details.

> **Authentication applies here too.** The A2A, MCP, and REST (`/api`) surfaces
> are gated by the same transport-tier auth as the browser / thin client: on a
> non-loopback bind they require the token (`?token=` or `Authorization: Bearer`);
> a same-machine UDS bind uses OS peer credentials instead. See the
> [`reyn web` reference § Authentication](../../reference/cli/web.md#authentication).

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

By default the server binds to `127.0.0.1` (localhost only). Run with `--host 0.0.0.0` to accept LAN connections, and configure `web.auth.token` in `reyn.yaml` first — a non-loopback bind refuses to start without one.

**`--connect` says "authentication required / rejected by the server"**

Pass `--token <secret>` (the token `reyn web` printed on launch, or your configured `web.auth.token`), or set `REYN_WEB_AUTH_TOKEN` in the environment.

---

## See also

- [Reference: CLI / chat](../../reference/cli/chat.md) — TUI slash commands, `--connect` / `--token` flags
- [Reference: AG-UI transport](../../reference/runtime/agui-transport.md) — the wire protocol `--connect` and the browser both use
- [Reference: reyn.yaml § web.auth](../../reference/config/reyn-yaml.md) — token / TLS / transport-tier config
- [Concepts: A2A](../../concepts/multi-agent/a2a.md) — agent-to-agent protocol
