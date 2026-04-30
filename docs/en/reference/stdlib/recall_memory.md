---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [recall_memory]
---

# `recall_memory`

Find memories relevant to a query.

## Entry

`pick`

## Final output

`memory_recall_result` — the top matches, loaded as full markdown bodies.

## How it works

1. Reads `MEMORY.md` indexes from each scope (global `~/.reyn/memory/`, per-project `.reyn/memory/`).
2. Asks the LLM to score entries by relevance to the query.
3. Loads the top matches and returns their content.

## Memory types

- `user` — facts about the user
- `feedback` — corrections or validated approaches
- `project` — project-specific context (deadlines, drivers, decisions)
- `reference` — pointers to external systems

Design rationale at `concepts/memory.md` (Phase 2).

## Source

[`src/stdlib/skills/recall_memory/skill.md`](https://github.com/<org>/reyn/blob/main/src/stdlib/skills/recall_memory/skill.md)
