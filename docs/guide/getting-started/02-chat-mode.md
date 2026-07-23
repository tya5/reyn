---
type: tutorial
topic: getting-started
audience: [human]
---

# 02 — Chat mode

`reyn chat` is the lowest-friction way to see Reyn in action — talk to it, watch it route your request to a skill, and read the answer back. No authoring required.

This tutorial uses only the auto-created `default` agent. Multi-agent setup is a later topic; pointers at the end.

## Start a session

```bash
reyn chat
```

The first time you run this, Reyn auto-creates a `default` agent under `.reyn/agents/default/`. Subsequent runs reuse it.

You'll get a `>` prompt.

## Type a turn

```
> summarize the README of this project
```

What happens:

1. The chat router classifies the intent.
2. It picks the best-matching skill — for a "summarize README" request, that's typically `read_local_files` followed by `direct_llm`, or `read_local_files` alone if the model already summarises inline.
3. The skill runs and the answer prints below the prompt.
4. The session stays open. Type the next turn.

Try a few:

```
> what is this project about?
> what's in src/reyn/?
> say hi in three languages
```

(To see the catalogue of skills the router picks from, exit and run `reyn skills` from the shell — chat conversations don't enumerate them.)

Each turn is logged under `.reyn/agents/default/`.

## Exit

`Ctrl+D` or `/quit` ends the session. Re-running `reyn chat` resumes the same agent — its memory and history persist.

## Web UI (optional)

While the TUI is running, you can also use a browser-based interface. In a second terminal:

```bash
reyn web
```

Then open **http://localhost:8080**. The Web UI connects to the same agent and shows the same session — richer rendering, better for longer conversations or screen sharing.

Stopping `reyn web` (Ctrl+C) doesn't affect the TUI session, and vice versa. See [How-to: Chat and Web UI](../for-users/chat-and-web-ui.md) for options.

## Slash commands

Lines starting with `/` are intercepted as control commands, not routed to the LLM:

- `/list` — show any pending user prompts (`ask_user` / permission asks).
- `/answer <id> <text>` — answer a pending prompt from `/list`.

These two are the only ones you need for default-mode use. More slash commands (`/agents`, `/attach`, `/rewind`, …) become useful once you have multiple agents or want to time-travel a conversation — see [reference/cli/chat](../../reference/cli/chat.md) when you get there.

## Memory is automatic

The router reads memory on every turn (no setup required) and writes durable facts back when it detects them. Two layers:

- **Shared** — `.reyn/memory/` — facts visible to every agent.
- **Agent** — `.reyn/agents/default/memory/` — facts scoped to this agent.

You can inspect what's been remembered:

```bash
reyn memory list
reyn memory show <slug>
```

See [concepts/memory](../../concepts/data-retrieval/memory.md) for the full model.

## What's actually happening

The OS doesn't know about "chat". It just runs the chat router, which picks a skill (or, in multi-agent setups, a peer agent) to delegate to. This is the same composition pattern any of your own skills would use (P7 (principles doc removed)).

## What you learned

- `reyn chat` attaches a REPL to the auto-created `default` agent.
- Each turn goes through the chat router, which picks a skill and runs it.
- Memory is two-layered (shared + agent) and read/written automatically.
- `/list`, `/answer` are the slash commands you need at this stage.

## Where to go next

You've seen Reyn deliver value as a chat agent. From here:

- **Multi-agent (later):** How-to: Build an agent team walks through `reyn agent new`, role-specific allowlists, and `/attach`. Background reading: [concepts/multi-agent](../../concepts/multi-agent/multi-agent.md), [concepts/topology](../../concepts/multi-agent/topology.md).
- **The why:** concepts/principles (principles doc removed).
