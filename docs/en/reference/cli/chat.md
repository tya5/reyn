---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn chat]
---

# `reyn chat`

Start an interactive REPL session. Each user turn is dispatched through the `skill_router` stdlib skill, which picks the best skill for the message and runs it. Memory recall and write happen automatically (controlled by `chat.memory` in `reyn.yaml`).

## Synopsis

```
reyn chat [OPTIONS]
```

## Options

| Flag | Description |
|------|-------------|
| `--chat-id ID` | Resume an existing chat by id. Default: a new id (a fresh `chats/<id>.json`). |
| `--model MODEL` | Model class or LiteLLM model string. Default from `reyn.yaml`. |
| `--output-language LANG` | Output language code. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per turn. `0` = unlimited. |

## Session state

Each session is persisted as JSON at `.reyn/chats/<chat_id>.json`. The file holds:

- conversation history (user/assistant turns)
- memory recall results that were injected into past turns
- per-turn token usage and skill selection

To continue a conversation later, pass the same `--chat-id`.

## Memory hooks

When `chat.memory.enabled: true` (the default), every turn:

1. **Recall**: the router preprocessor calls `recall_memory` and surfaces the top-`recall_top_k` matching memories to the chosen skill.
2. **Write**: after every `chat.memory.turn_threshold` turns (or every `chat.memory.time_threshold` seconds), the session offers `write_memory` a chance to persist anything new.

Both knobs are configured in `reyn.yaml`:

```yaml
chat:
  memory:
    enabled: true
    global_enabled: false   # opt in to ~/.reyn/memory (cross-project)
    turn_threshold: 4
    time_threshold: 600
    recall_top_k: 5
```

### Memory scopes

Memory lives in two places:

| Scope | Path | Default |
|-------|------|---------|
| Project | `./.reyn/memory/` | always on (under CWD, no permission prompt) |
| Global | `~/.reyn/memory/` | **off** — set `chat.memory.global_enabled: true` to enable |

Global memory persists facts across projects (e.g. `User Role`, long-running preferences). Because `~/.reyn/` is outside the project root, enabling it triggers a one-time permission prompt at chat startup; the approval is persisted to `.reyn/approvals.yaml`.

## Permission behavior

`reyn chat` is interactive: when a sub-skill needs a permission outside the defaults, the prompt blocks until you respond. Choices can be persisted to `.reyn/approvals.yaml` (see [permissions reference](../config/permissions.md)).

## Examples

Start a new session:

```bash
reyn chat
```

Resume a previous session:

```bash
reyn chat --chat-id 2026-04-30-1430-abc
```

Use a stronger model just for this conversation:

```bash
reyn chat --model strong
```

## See also

- [Reference: stdlib/skill_router](../stdlib/skill_router.md)
- [Reference: stdlib/recall_memory](../stdlib/recall_memory.md)
- [Reference: stdlib/write_memory](../stdlib/write_memory.md)
- [Reference: state-dir](../config/state-dir.md) — `chats/` location
- [Concepts: memory](../../concepts/memory.md)
