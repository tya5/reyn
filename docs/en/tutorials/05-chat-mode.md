---
type: tutorial
topic: getting-started
audience: [human]
---

# 05 — Chat mode

`reyn chat` is an interactive REPL. Each turn picks a skill via `skill_router` and runs it. Memory is saved automatically; future runs (chat or otherwise) recall it.

## Start a session

```bash
reyn chat
```

Type a turn:

```
> summarize the README of this project
```

The router picks `text_summarizer` (or whatever stdlib/project skill best matches), runs it, and prints the result. Each turn stays in the same session.

## How the router picks

`skill_router` reads the user message, your project's available skills, and recent memory. It picks one and runs it. If you want to force a particular skill, ask explicitly: "use skill_builder to ..." — the router uses the cue.

## Memory is automatic

By default, `reyn chat`:

1. **Recalls** matching memory before each turn (top-K entries are surfaced to the chosen skill).
2. **Writes** new memory every few turns (governed by `chat.memory.turn_threshold` / `time_threshold`).

Turn it off in `reyn.yaml` if you want a memoryless session:

```yaml
chat:
  memory:
    enabled: false
```

## Sessions are persistent

Every chat lands at `.reyn/chats/<chat_id>.json`. Resume later:

```bash
reyn chat --chat-id 2026-04-30-1430-abc
```

The full conversation history, recall results, and per-turn metadata replay into the new process.

## Inspecting and managing memory

```bash
reyn memory list             # show all stored memories
reyn memory show <slug>      # print one
reyn memory edit <slug>      # open in $EDITOR
reyn memory delete <slug>    # remove
```

## Why chat mode is just a router skill

The OS doesn't know about "chat" — it just runs a skill. `skill_router` is a normal stdlib skill that happens to choose another skill to delegate to. This is the same composition pattern as any other reyn skill (P7).

## What you learned

- `reyn chat` is `skill_router` in a REPL.
- Memory recall and write happen on a configurable cadence.
- Sessions persist; resume by `--chat-id`.

## Where to go next

You've covered: skill creation, running, eval, chat. From here:

- **Build something real.** Replace one of your prompt-based workflows with a multi-phase skill.
- **Read the [concepts](../concepts/principles.md).** Understanding the eight principles makes everything in the reference make sense.
- **Browse [how-to](../how-to/validate-artifacts.md).** Pick a guide for whatever specific need comes up first.
