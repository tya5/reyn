---
type: tutorial
topic: getting-started
audience: [human]
---

# 02 — Chat mode

`reyn chat` is the lowest-friction way to see Reyn in action — talk to it, watch it route your request to a stdlib skill, and read the answer back. No authoring required.

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

1. `skill_router` (a stdlib skill) classifies the intent.
2. It picks the best-matching skill — for a "summarize README" request, that's typically `read_local_files` followed by `text_summarizer`, or `read_local_files` alone if the model already summarises inline.
3. The skill runs and the answer prints below the prompt.
4. The session stays open. Type the next turn.

Try a few:

```
> what skills are available?
> what's in src/reyn/?
> say hi in three languages
```

Each turn is logged under `.reyn/agents/default/`.

## Exit

`Ctrl+D` or `/quit` ends the session. Re-running `reyn chat` resumes the same agent — its memory and history persist.

## Slash commands

Lines starting with `/` are intercepted as control commands, not routed to the LLM:

- `/list` — show currently running skill spawns and any pending user prompts.
- `/cancel <id>` — cancel a running skill spawn (id from `/list`).
- `/answer <id> <text>` — answer a pending `ask_user` or permission prompt.

These three are the only ones you need for default-mode use. More slash commands (`/agents`, `/attach`, `/plan`, …) become useful once you have multiple agents or long-running plans — see [reference/cli/chat](../../reference/cli/chat.md) when you get there.

## Memory is automatic

The router reads memory on every turn (no setup required) and writes durable facts back when it detects them. Two layers:

- **Shared** — `.reyn/memory/` — facts visible to every agent.
- **Agent** — `.reyn/agents/default/memory/` — facts scoped to this agent.

You can inspect what's been remembered:

```bash
reyn memory list
reyn memory show <slug>
```

See [concepts/memory](../../concepts/memory.md) for the full model.

## What's actually happening

The OS doesn't know about "chat". It just runs a skill — `skill_router` — that happens to pick another skill (or, in multi-agent setups, a peer agent) to delegate to. The router skill is a normal stdlib skill, not special tooling. This is the same composition pattern any of your skills would use ([P7](../../concepts/principles.md#p7-os-is-skill-agnostic-critical)).

## What you learned

- `reyn chat` attaches a REPL to the auto-created `default` agent.
- Each turn goes through `skill_router`, which picks a stdlib skill and runs it.
- Memory is two-layered (shared + agent) and read/written automatically.
- `/list`, `/cancel`, `/answer` are the slash commands you need at this stage.

## Where to go next

You've seen Reyn deliver value as a chat agent. From here:

- **[Tutorial 03 — Your first skill](03-your-first-skill.md)** — author a skill of your own with `skill_builder`.
- **[Tutorial 04 — Running a skill](04-running-a-skill.md)** — run a skill from the CLI in depth (input formats, flags, event log).
- **[Tutorial 05 — Writing an eval](05-writing-an-eval.md)** — pin behaviour with a rubric.
- **Multi-agent (later):** [How-to: Build an agent team](../for-skill-authors/build-an-agent-team.md) walks through `reyn agent new`, role-specific allowlists, and `/attach`. Background reading: [concepts/multi-agent](../../concepts/multi-agent.md), [concepts/topology](../../concepts/topology.md).
- **The why:** [concepts/principles](../../concepts/principles.md).
