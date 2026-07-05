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

Common runtime flags (`--model`, `--output-language`, `--phase-budget`, `--llm-timeout`, `--llm-max-retries`) are shared with `reyn run` and `reyn eval`. See [Common flags](common-flags.md).

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
| `--connect <WS_URL>` | off | Connect to a remote `reyn web` server over WebSocket (e.g. `--connect ws://localhost:8080`). The positional `agent_name` selects which agent on the server. Requires `pip install reyn[web]`. Right panel features that need local file access render in "remote — limited" v1 mode. |

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
| `/cancel <id-prefix>` | Cancel a running skill (accepts any unique prefix of the run_id) |
| `/clear-history` | Wipe chat history (**destructive**; clears in-memory + persistent history) |
| `/compact` | Compact the conversation history now to free up the context window (see [chat-compaction](../../concepts/data-retrieval/chat-compaction.md)) |
| `/concept <term>` | Inline glossary lookup (T1-3) |
| `/copy [N\|list]` | Copy an agent reply to the clipboard (1 = newest, 2 = one turn back, …) |
| `/cost` | Quick token + USD cost summary for this agent |
| `/exit` | Exit the chat (alias: `/quit`, Ctrl+D) |
| `/help [<cmd>]` | Slash command help — list all, or focus on one |
| `/image <path>` | Attach an image to the next user message (multimodal input) |
| `/list` | List running skills and pending interventions |
| `/memory [list\|view <name>]` | Inspect project memory entries (see [concepts/memory](../../concepts/data-retrieval/memory.md)) |
| `/model [<class>]` | Show the session's model class and any override, or set a per-session model-class override with `/model <class>` (validated against known classes; clears on restart) |
| `/pending [list\|discard <id>\|claim <id>]` | List / discard / claim stalled cross-channel ops |
| `/quit` | Exit the chat (alias: `/exit`, Ctrl+D) |
| `/reset confirm` | Reset in-flight skill state (snapshots + WAL; audit logs preserved) |
| `/rewind [seq]` | Time-travel to an earlier checkpoint — no arg opens the picker menu; `seq` jumps directly (see [Time-travel](../../concepts/runtime/time-travel.md) · [How-to](../../guide/for-users/time-travel.md)) |
| `/session new \| switch <sid> \| list` | Open / switch / list conversation sessions for the attached agent (see [Sessions](../../concepts/multi-agent/sessions.md)) |
| `/skill list` | Show active skill runs (id, name, current phase + parent lineage) |
| `/skill discard <run_id>` | Abort a specific skill run + cleanup |
| `/skills` | List available skills (stdlib, project, local) |
| `/tasks` | Unified view of active skill runs. Same as `/tasks list` |
| `/tasks status <prefix>` | Show current phase + elapsed for a specific skill run |
| `/tasks kill <prefix>` | Cancel a specific skill run; prefix matches against skill run_ids |

`/list` / `/cancel` / `/answer` are foundational — they let multiple skill runs and interventions coexist without blocking the prompt. `/agents` / `/attach` / `/agent` are the multi-agent workflow primitives. `/skill` is the crash-recovery operator command that surfaces the per-skill-run lifecycle: inspect what is running or abort a stuck run. `/tasks` is the unified entry point for skill runs — the LLM also points users at `/tasks` after a skill is spawned. `/copy` is a conversation-pane utility; `/image` enables multimodal input.

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

