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

> **Security note**: The default `127.0.0.1` binding accepts connections from localhost only. A non-loopback bind (like `--host 0.0.0.0`) refuses to start unless a bearer token is configured (`gateway.auth.token` in `reyn.yaml`) — the gateway never exposes itself to the network unauthenticated.

---

## Remote thin client (`reyn chat --connect`)

`reyn chat --connect` attaches a thin terminal client to a `reyn web` server someone else is running (or one you started earlier) — the server holds the session; your terminal just streams the conversation and relays your input.

### Start the server

```bash
reyn web --host 0.0.0.0 --port 8080
```

On the default loopback bind (`127.0.0.1`), reyn generates a launch token and prints it in the startup URL (`http://127.0.0.1:8080/?token=...`). On a non-loopback bind, reyn refuses to start unless you've configured `gateway.auth.token` in `reyn.yaml` — copy that token (or the printed URL) before connecting from elsewhere.

### Connect

```bash
reyn chat --connect http://<host>:8080 --token <secret> [agent_name]
```

`--connect`'s AG-UI client dependencies are core (#5051) — no separate install step.

- `agent_name` is optional and picks which agent on the server you attach to (same as local `reyn chat <agent_name>`).
- `--token` can be omitted if `REYN_WEB_AUTH_TOKEN` is already set in your environment.
- The connection is plain HTTP + Server-Sent Events (AG-UI) — there's nothing else to open or forward besides the one port.

### What you get

Replies, tool activity, and status stream in as they happen on the server. A human-in-the-loop prompt (a permission ask, a clarifying question) can be answered from the remote terminal exactly like a local one — your answer is delivered to the server by id, so it lands correctly even with other clients attached to the same agent.

**Multiple clients see each other's turns.** If two or more clients (a local `reyn chat` and one or more `--connect` terminals, or several `--connect` terminals) attach to the same agent, everyone sees the full conversation — not just the agent's replies. Whoever types a message or answers a human-in-the-loop prompt, every OTHER attached client sees that line too, tagged with who sent it (e.g. `user [alice]:`) when more than one identity is attached; a single attached client shows the plain line with no tag.

### What's different from local `reyn chat`

- **Same inline CUI, streamed status bar.** On an interactive TTY, `--connect` renders the same inline CUI as local `reyn chat`, including the main status bar — `model` / `agent` / `cost` / `ctx%` chips and the working indicator — with those values streamed live from the server. (`--cui`, a non-TTY, or piped output still falls back to the plain console style, exactly like local.)
- **Status-bar *dropdowns*, the `task` chip, and pickers are local-only.** The streamed chip *values* render, but opening a chip's dropdown (the cost/context detail, the `/model` class picker, the agent / task tree, the `…` overflow toggles) shows an empty panel on a remote attach — that detail is session-local and not on the wire. The `task` chip shows `0` (the task count is not streamed). A closed-set human-in-the-loop prompt (a permission `[y]es` / `[n]o`) is answered by **typing** the choice on the input line rather than through the ↑↓ region picker.
- **`/rewind` is a text list, not the picker.** Locally `/rewind` opens an interactive ↑↓ checkpoint picker; over `--connect` it prints the same checkpoints as a plain text list instead (the picker is a local region, not carried on the wire — rewind with `/rewind <seq>`).
- **No local file access.** `--connect` is a pure transport client — it never touches a local session, workspace, or tool. Everything runs on the server's machine.

### Security notes

- For same-machine thin-client use, prefer a UNIX domain socket instead of a token: `reyn web --uds /path/to/socket` — the connection is authenticated by OS peer credentials, no token needed.
- Any network bind (anything other than loopback) always requires `gateway.auth.token` and runs over TLS (self-signed by default; reyn prints the certificate fingerprint to pin on first connect).
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

If the agent halts on a persistent durability failure (extremely rare — a dead
disk or an unwritable state directory), the ALWAYS-VISIBLE status bar shows a
`⚠ HALTED — <reason>` banner ahead of the usual chips the moment it happens, so
you learn it even if you're not actively typing. The plain `--cui` renderer
shows the same message in its bottom toolbar. This is a notification only —
the agent has already stopped accepting new operations synchronously, whether
or not you see the banner.

### Input

| Key | Action |
|-----|--------|
| `Enter` | Send the current prompt — **always**, even with the completion menu open (it never accepts a suggestion on your behalf) |
| `Shift+Enter` | Insert a newline |
| `Ctrl+J` | Insert a newline — the guaranteed-works fallback on any terminal, for pasting or writing a multi-line prompt |
| `↑` | On the first line: focus a pending question, else the sent-message queue. Otherwise move the cursor up |
| `↓` | On the last line: focus the menu row below the input. Otherwise move the cursor down |
| `F2` | Start/stop voice dictation — transcribed text lands at the cursor, never auto-sent. Requires `pip install "reyn[voice]"`; see [Concepts: Voice input](../../concepts/tools-integrations/voice.md) |

#### Completing `/` commands and `:` skills

Type `/` at the start of the input to open the command menu, or `:` at the start
of the input to open the skill menu — both list everything with nothing else
typed. A `:` **later in the line** needs to start a word and be followed by at
least two characters, so an ordinary `12:30` or `http://x` never opens a menu.
Once a command's name is settled by a space, the menu switches to completing that
command's **argument** — so `/model ` lists your configured model classes and
`/image ` lists matching files.

Most commands take arguments but have no list to offer. For those, that same
space shows the command's **usage line** instead — `/visibility ` puts
`↳ usage: /visibility on|off <tool|mcp|category> <name>` above the input, so you
can see what to type without breaking off to run `/help visibility`. A usage
hint is only a hint: it takes no keys, so `↑`, `Tab` and `Esc` keep their normal
meanings while it is up. Commands that take no arguments at all (`/cost`,
`/list`, `/quit`, …) show nothing.

While the menu is open **with suggestions in it**, it takes over the arrow keys:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move the highlighted suggestion |
| `Tab` | Accept the highlighted suggestion |
| `Esc` | Dismiss the menu. It stays dismissed while you keep typing that same word — start a new `/` or `:` word to bring it back |
| `Enter` | Send what you typed (never accepts a suggestion) |

With the menu closed, `↑`, `↓`, `Tab` and `Esc` all behave exactly as in the
table above. Over `--connect`, everything that comes from the command registry —
`/` command names and usage lines — still works, because every client has that
registry; argument suggestions and `:` skill completion are silent, since both
read session-local state that is not on the wire.

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
| `Ctrl+L` | Toggle a full-viewport text effect over the conversation (a joke) — `Ctrl+L` starts it; any key press or scroll (not only `Ctrl+L`) stops it, and stopping restores the exact prior view. Needs the optional `effects` extra (`pip install 'reyn[effects]'`); without it, pressing the key shows a status message naming the install rather than doing nothing |

### Conversation pane (interactive TTY)

Two ways to interact directly with the conversation history, not just the input line — details in [feature-map: Textual TUI conversation-pane interaction](../../feature-map.md) and [AG-UI transport reference](../../reference/runtime/agui-transport.md):

| Key | Action |
|-----|--------|
| `Ctrl+N` | Open the in-conversation search bar. Incremental, case-insensitive substring match; `Enter`/`↑` = older match, `Shift+Enter`/`↓` = newer, both wrapping; `Esc` closes the bar and returns focus to the input. Searching moves the same cursor `Shift+Tab`/`Ctrl+O` uses, and closing the bar leaves it on the match you found — so either pane-focus key picks up from there |
| `Ctrl+O` / `Shift+Tab` | Move focus into the conversation pane itself (`Ctrl+O` is a direct jump; `Shift+Tab` also cycles through the rest of the chrome), landing on the entry you left it on — or, on first entry, the newest one. `Esc` returns focus to the input |
| `c` | (with the conversation pane focused) Show/hide a vim-style text cursor over the content, for selecting and copying part of a reply — always live, no mode to enter or leave. `hjkl` move, `v`/`V` select, `y` yanks, `Esc` cancels the selection; `*`/`n`/`N` search the selection. These are the library's own keys |

Once the pane holds focus (via `Ctrl+O` or `Shift+Tab`), the cursor moves with `↑`/`↓`/`PageUp`/`PageDown`/`Home`/`End`:

| Key | Action |
|-----|--------|
| `Enter` | Copy the cursor's entry text to the clipboard |
| `Space` | Fold/unfold the highlighted entry's tool detail (#4697 — decoupled from highlight movement, which no longer auto-expands/folds). Inside the vim-style text cursor (`c`, above), falls through to the same copy as `Enter` instead, so an in-progress text selection is never disrupted |
| `r` | Send a bare `/rewind` (the same as typing it) — not a jump to that specific entry |

### Working indicator

The `turn_started` → `turn_settled` event pair (consumed by
`ChatRenderer.on_audit_event`) is the sole signal that drives the
turn-in-progress ("thinking…") indicator — there is no separate, manually
managed status line. An earlier version had one, and it double-displayed
against the event-driven indicator (both showing at once) and could leave an
orphaned blank line behind once cleared. Relying on the single event pair
instead of a second, hand-maintained status line is what keeps the indicator
consistent with the renderer's own state.

> Slash commands are documented in the
> [`reyn chat` reference](../../reference/cli/chat.md#slash-commands).

### Config-warning indicator

If `reyn.yaml` / `reyn.local.yaml` / `~/.reyn/config.yaml` contains a key
`reyn` does not recognize (a typo, or a key from a since-renamed config
version), it is not applied — and a one-line indicator appears in the
bottom chrome, above the menu row, naming how many keys were skipped:

```
⚠ 2 config keys not applied → reyn config validate
```

It stays visible for the whole session (fixing this requires a restart —
`reyn.yaml` is read once at startup) and never scrolls away with the
conversation. Run `reyn config validate` for the detail this line
deliberately omits: which key, why, and — for a renamed key — the exact new
name to use. This covers `reyn.yaml`'s operator-editable config only; the
separately hot-reloaded `.reyn/*.yaml` registries (mcp/cron/skills/
pipelines/presentations) warn to the log on the same unknown-key check but
are not (yet) reflected in this indicator.

---

## Choosing which surfaces are hosted (`--enable` / `--disable`)

`reyn web` hosts several surfaces at once. By default it exposes just what a
browser/CLI operator needs: the **AG-UI** chat transport, the **Web UI**
(the browser shell), the REST **`/api`** control plane, **`/health`**, and
the resource-fetch routes. Two broad machine-integration surfaces —
**A2A** and **MCP** — are **off by default** and must be opted in explicitly:

```bash
reyn web --enable a2a              # turn on the A2A JSON-RPC endpoint
reyn web --enable a2a --enable mcp # turn on both (repeat the flag per surface)
reyn web --disable api             # turn off a surface that's on by default
```

The same toggles are settable in `reyn.yaml` under `gateway.surfaces` (see the
[`reyn.yaml` reference § gateway.surfaces](../../reference/config/reyn-yaml.md#gatewaysurfaces-per-surface-opt-inopt-out-fp-0058-p2))
so an operator running the same project repeatedly doesn't need to repeat
the CLI flags every launch. A `--enable`/`--disable` flag on the command
line always wins over the config file.

---

## A2A endpoint (advanced)

The web server can also expose an [A2A](../../concepts/multi-agent/a2a.md) JSON-RPC endpoint for programmatic access and agent-to-agent communication — **opt-in**, start the server with `--enable a2a` (see above):

```bash
reyn web --enable a2a
```

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

By default the server binds to `127.0.0.1` (localhost only). Run with `--host 0.0.0.0` to accept LAN connections, and configure `gateway.auth.token` in `reyn.yaml` first — a non-loopback bind refuses to start without one.

**`--connect` says "authentication required / rejected by the server"**

Pass `--token <secret>` (the token `reyn web` printed on launch, or your configured `gateway.auth.token`), or set `REYN_WEB_AUTH_TOKEN` in the environment.

**A2A / MCP requests 404**

A2A and MCP are off by default (secure-default; see [Choosing which surfaces are hosted](#choosing-which-surfaces-are-hosted-enable-disable) above). Start the server with `--enable a2a` / `--enable mcp`, or set `gateway.surfaces.a2a.enabled: true` / `gateway.surfaces.mcp.enabled: true` in `reyn.yaml`.

---

## See also

- [Reference: CLI / chat](../../reference/cli/chat.md) — TUI slash commands, `--connect` / `--token` flags
- [Reference: AG-UI transport](../../reference/runtime/agui-transport.md) — the wire protocol `--connect` and the browser both use
- [`reyn.yaml` reference § gateway.surfaces](../../reference/config/reyn-yaml.md#gatewaysurfaces-per-surface-opt-inopt-out-fp-0058-p2) — the full per-surface secure-default table and precedence rules
- [Reference: reyn.yaml § gateway.auth](../../reference/config/reyn-yaml.md) — token / TLS / transport-tier config
- [Concepts: A2A](../../concepts/multi-agent/a2a.md) — agent-to-agent protocol
