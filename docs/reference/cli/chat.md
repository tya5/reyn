---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn chat]
---

# `reyn chat`

Start an interactive REPL session attached to an agent. Each user turn is dispatched through the `skill_router` stdlib skill, which classifies the intent and either replies directly, runs a project / stdlib skill, or delegates to another agent.

Memory recall and write happen automatically inside the router phase — see [concepts/memory](../../concepts/data-retrieval/memory.md).

## Synopsis

```
reyn chat [agent_name] [OPTIONS]
```

`agent_name` is positional and optional. When omitted, reyn attaches to the auto-created `default` agent.

## Options

Common runtime flags (`--model`, `--output-language`, `--phase-budget`, `--llm-timeout`, `--llm-max-retries`) are shared with `reyn run-once`. See [Common flags](common-flags.md).

Chat-specific flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--cui` | off | Use plain console output (no TUI). Useful for piping output, debugging, or headless environments. |
| `--no-restore` | off | Skip restoring in-flight skill state from disk this run. Useful for debugging or starting a clean session. |
| `--reset` | off | Wipe in-flight skill state (snapshots + WAL) before starting. Audit logs in `.reyn/events/` are preserved. |
| `--banner` | off | Show the ASCII-art startup banner (gradient REYN logo + agent / model info). |
| `--eager-embedding-build` | off | Await action embedding index build synchronously on the first turn (pays ~2–5 s once so `search_actions` is immediately available). |
| `--grant-file-write` | off | Grant `file.read`/`file.write` at the resolver layer for this session, scoped to the sandbox write zone. For non-interactive/scripted use — when you know the agent will need to edit the working tree and want to avoid the per-skill permission prompt. |
| `--exclude-tools NAMES` | — | Comma-separated tool names to hide from the agent's LLM-visible catalog (e.g. `web__search,web__fetch`). The tools still exist and still run if the agent calls them by name; they are just not offered to the model's discovery surface this session. |
| `--connect <URL>` | off | Attach to a remote `reyn web` server over AG-UI (HTTP+SSE) instead of running a local session (e.g. `--connect http://127.0.0.1:8080`). The positional `agent_name` selects which agent on the server. Requires `pip install reyn[web]`. Renders the SAME way as a local session: the inline CUI on an interactive TTY (with the main status bar — agent / model / cost / ctx% / task / working indicator — streamed over the wire), or the plain console for `--cui` / a non-TTY / piped output. Status-bar *dropdowns* and the interactive intervention / `/rewind` pickers are session-local and degrade to empty/text on a remote attach (a remote closed-set intervention is answered by typing on the input line; `/rewind` shows a plain text list). See [how-to: remote thin client](../../guide/for-users/chat-and-web-ui.md#remote-thin-client-reyn-chat-connect). |
| `--token <SECRET>` | off | Bearer token for `--connect` (the secret `reyn web` prints on launch, or a token configured via `web.auth.token`). Falls back to the `REYN_WEB_AUTH_TOKEN` env var. A same-machine UDS server may need none. |

## Agent workspace

Each agent persists state under `.reyn/agents/<name>/`:

- `profile.yaml` — name, role, optional `allowed_mcp`
- `history.jsonl` — append-only conversation log (chat + agent-to-agent messages, with chain_id for cross-agent trace)
- `events.jsonl` — runtime events for `reyn events`
- `memory/` — agent-scoped memory layer (`MEMORY.md` + body files)
- `runs/` — workspaces for spawned skill runs

To resume a previous conversation, attach to the same agent:

```bash
reyn chat researcher
```

The `default` agent always exists. Create more with [`reyn agent new`](agent.md).

## Slash commands

While a session is active, lines starting with `/` are intercepted and never routed to an agent.

| Command | Effect |
|---------|--------|
| `/agent edit role <text>` | Rewrite the attached agent's persona |
| `/agent new <name>` | Create new agent and attach to it |
| `/agents` | List loaded agents and which one is currently attached |
| `/answer <id-prefix> <text>` | Answer a pending `ask_user` / permission prompt (id-prefix: any unique prefix of the intervention id) |
| `/attach <name>` | Switch the REPL pointer to another agent (the previous one keeps running in the background) |
| `/budget [reset]` | Full budget breakdown; `/budget reset` clears per-process counters (see [config/budget](../config/budget.md)) |
| `/clear-history` (alias `/clear`) | Wipe chat history (**destructive**; clears in-memory + persistent history and the action-usage table; events/run-state/profile preserved) |
| `/compact` | Compact the conversation history now to free up the context window (see [chat-compaction](../../concepts/data-retrieval/chat-compaction.md)) |
| `/concept <term>` | Inline glossary lookup |
| `/copy [N\|list]` | Copy an agent reply to the clipboard (1 = newest, 2 = one turn back, …) |
| `/cost` | Quick token + USD cost summary for this agent |
| `/exit` | Exit the chat (alias: `/quit`, Ctrl+D) |
| `/help [<cmd>]` | Slash command help — list all, or focus on one |
| `/hook on\|off <name>` | Enable/disable a hook for this session (live at the next dispatch; session-scoped — the hook still fires in the agent's other sessions; persists across restart) |
| `/image <path>` (alias `/img`) | Attach an image to the next user message (multimodal input; png/jpg/jpeg/gif/webp/svg) |
| `/list` | List pending interventions |
| `/memory [list\|view <name>]` | Inspect project memory entries (see [concepts/memory](../../concepts/data-retrieval/memory.md)) |
| `/model [<class>]` | Show the session's model class and any override, or set a per-session model-class override with `/model <class>` (validated against known classes; clears on restart) |
| `/pending [list\|discard <id>\|claim <id>]` | List / discard / claim stalled cross-channel ops |
| `/quit` | Exit the chat (alias: `/exit`, Ctrl+D) |
| `/reload` | Hot-reload runtime config (`.reyn/*.yaml`) at the next turn boundary |
| `/reset confirm` | Reset in-flight run state (snapshots + WAL; audit logs preserved) |
| `/rewind [seq]` | Time-travel to an earlier checkpoint — no arg opens the picker menu; `seq` jumps directly (see [Time-travel](../../concepts/runtime/time-travel.md) · [How-to](../../guide/for-users/time-travel.md)) |
| `/session new \| switch <sid> \| list` | Open / switch / list conversation sessions for the attached agent (see [Sessions](../../concepts/multi-agent/sessions.md)) |
| `/tasks [list]` | List dynamic tasks the LLM created via `task__create` |
| `/tasks status <task_id-prefix>` | Show a task's status + dependencies |
| `/tasks kill <task_id-prefix>` | Abort a specific dynamic task |
| `/visibility on\|off <tool\|mcp\|category> <name>` | Toggle this session's LLM visibility of a capability (hidden next turn / restored up to the agent's authorized envelope — an envelope-denied capability stays hidden) |

`/list` / `/answer` are foundational — they let pending interventions coexist without blocking the prompt. `/agents` / `/attach` / `/agent` are the multi-agent workflow primitives. `/tasks` is the entry point for dynamic tasks the LLM spawns via `task__create` — list what's running, inspect a specific one's status/dependencies, or kill it; the LLM also points users at `/tasks` after creating one. `/hook` / `/visibility` are session-scoped LLM-catalog controls, mirroring the status bar's `hook`/`tool`/`mcp`/`category` chips. `/copy` is a conversation-pane utility; `/image` enables multimodal input.

## Multi-agent behavior

If the router decides this turn would be better handled by another agent, it emits a `messages_to_agents` entry instead of (or in addition to) a `skills_to_run` entry. The receiving agent processes the request asynchronously; its reply is auto-routed back into the originating chain. See [concepts/multi-agent](../../concepts/multi-agent/multi-agent.md) for the full model.

A user-initiated chain emits an interim `reply_text` (the originating agent's first router turn) followed by a synthesized final reply (after delegate responses arrive). This preserves the "you'll see I'm working on it" UX even across hops.

The `/attach` slash lets you watch a delegate's progress mid-chain — the previous agent's `session.run()` keeps consuming its inbox, so coming back later still resolves cleanly.

## Permission behavior

`reyn chat` is interactive: when a sub-skill needs a permission outside the defaults, the prompt blocks until you respond via the intervention queue. Choices can be persisted to `.reyn/approvals.yaml` (see [permissions reference](../config/permissions.md)).

## Examples

Start a new session against the default agent:

```bash
reyn chat
```

Attach to a named agent:

```bash
reyn chat researcher
```

Use a stronger model just for this conversation:

```bash
reyn chat --model strong
```

## See also

- [Reference: agent CLI](agent.md) — `reyn agent new / list / show / rm`
- [Reference: topology CLI](topology.md) — `reyn topology` to declare communication structure
- [Reference: multi-agent config](../config/multi-agent.md) — `safety.loop.max_agent_hops`
- [Reference: state-dir](../config/state-dir.md) — `agents/` location
- [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md)
- [Concepts: memory](../../concepts/data-retrieval/memory.md)

