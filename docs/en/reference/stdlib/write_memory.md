---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [write_memory]
---

# `write_memory`

Extract durable memories from a conversation segment and persist them to the appropriate scope.

## Entry

`extract`

## Final output

`memory_extract_result` — list of memory files written and the scope chosen.

## How it works

1. Scans the conversation for facts that should outlive the current session (user role, preferences, project state, external pointers).
2. Decides between global scope (`~/.reyn/memory/`) and project scope (`.reyn/memory/`).
3. Writes one markdown file per memory + appends an entry to `MEMORY.md`.

## When NOT to extract

- Code patterns, conventions, paths — these are derivable from the codebase.
- Git history — `git log` is authoritative.
- Ephemeral task state.

Full extraction rules at `concepts/memory.md` (Phase 2).

## Source

[`src/stdlib/skills/write_memory/skill.md`](https://github.com/<org>/reyn/blob/main/src/stdlib/skills/write_memory/skill.md)
