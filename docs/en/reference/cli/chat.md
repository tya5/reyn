---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn chat]
---

# `reyn chat`

Start an interactive REPL session attached to an agent. Each user turn is dispatched through the `skill_router` stdlib skill, which classifies the intent and either replies directly, runs a project / stdlib skill, or delegates to another agent.

Memory recall and write happen automatically inside the router phase — see [concepts/memory](../../concepts/memory.md).

## Synopsis

```
reyn chat [agent_name] [OPTIONS]
```

`agent_name` is positional and optional. When omitted, reyn attaches to the auto-created `default` agent.

## Options

| Flag | Description |
|------|-------------|
| `--model MODEL` | Model class or LiteLLM model string for this session. Default from `reyn.yaml`. |
| `--output-language LANG` | Output language code. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per turn. `0` = unlimited. |

## Agent workspace

Each agent persists state under `.reyn/agents/<name>/`:

- `profile.yaml` — name, role, optional `allowed_skills` ([reference](../dsl/profile-yaml.md))
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
| `/list` | Show running skill spawns and pending interventions |
| `/cancel <id>` | Cancel a skill spawn (full id or last 4 chars) |
| `/answer <id> <text>` | Answer a pending `ask_user` / permission prompt |
| `/agents` | List loaded agents and which one is currently attached |
| `/attach <name>` | Switch the REPL pointer to another agent (the previous one keeps running in the background) |

`/list` / `/cancel` / `/answer` are foundational — they let multiple skill runs and interventions coexist without blocking the prompt. `/agents` / `/attach` are the multi-agent workflow primitives.

## Multi-agent behavior

If the router decides this turn would be better handled by another agent, it emits a `messages_to_agents` entry instead of (or in addition to) a `skills_to_run` entry. The receiving agent processes the request asynchronously; its reply is auto-routed back into the originating chain. See [concepts/multi-agent](../../concepts/multi-agent.md) for the full model.

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
- [Reference: skill_router](../stdlib/skill_router.md)
- [Reference: profile-yaml](../dsl/profile-yaml.md)
- [Reference: multi-agent config](../config/multi-agent.md) — `multi_agent.max_hop_depth`
- [Reference: state-dir](../config/state-dir.md) — `agents/` location
- [Concepts: multi-agent](../../concepts/multi-agent.md)
- [Concepts: memory](../../concepts/memory.md)
