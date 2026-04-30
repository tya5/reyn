---
type: concept
topic: architecture
audience: [human, agent]
---

# Memory

Memory is reyn's mechanism for facts that should outlive a single run: user preferences, project-specific conventions, prior decisions. Two stdlib skills (`recall_memory`, `write_memory`) are the only entry points — there is no separate memory API in the OS.

## Two scopes

| Scope | Lives at | Purpose |
|-------|----------|---------|
| Global | `~/.reyn/memory/` | Facts about the user (role, preferences) — shared across all projects |
| Project | `.reyn/memory/` | Facts about this project (conventions, decisions, current state) |

Both scopes share the same shape: a `MEMORY.md` index plus one `<slug>.md` file per entry. Both are read together by `recall_memory` (project entries surface first).

## Symmetry with docs

The relationship between memory and docs is intentional:

| Memory | Docs |
|--------|------|
| What the system has learned about *this* user/project | What the system *can do* in general |
| Read by `recall_memory` (stdlib) | Read by `recall_docs` (planned, not yet implemented) |
| Persisted across runs | Static |

Both fit the same shape: a stdlib skill that the OS does not need to know about. Adding new memory or doc kinds doesn't change OS code.

## When to write memory

Write memory for facts that will be useful **in future conversations**:

- The user's role and ways they want to collaborate
- Feedback they've given you (corrections AND validations)
- Project context that's not derivable from `git log` or the codebase
- Pointers to where information lives (Linear projects, Slack channels, dashboards)

Don't write memory for:

- Code patterns or architecture (read the code)
- The current task's progress (use the task list / plan)
- Things already documented (read the docs)

## When to recall memory

Recall is automatic in `reyn chat` (every turn, if `chat.memory.enabled`). Skills can also opt in via a preprocessor:

```yaml
preprocessor:
  - run_skill:
      skill: recall_memory
      input:
        type: user_message
        data: { text: "what does the user prefer?" }
      into: relevant_memories
```

The fetched entries land at `input.relevant_memories` and the LLM uses them like any other input field.

## Staleness

Memory is a snapshot in time. A "feedback" entry from six months ago may no longer apply; a "project" entry that names a file path may be wrong if the file moved. Skills that read memory should verify before acting on specifics.

The system does not auto-decay or expire entries. Pruning is the user's responsibility (`reyn memory delete`, `reyn memory edit`).

## Where memory differs from events

| | Memory | Events |
|---|--------|--------|
| Across-run state? | Yes | No (per-run) |
| Author | The user (or `write_memory` on the user's behalf) | The OS |
| Format | Markdown w/ frontmatter | JSONL |
| Read by | `recall_memory` skill | `reyn events` CLI |

Events answer "what happened in this run?"; memory answers "what should I know going into the next run?"

## See also

- [Reference: stdlib/recall_memory](../reference/stdlib/recall_memory.md)
- [Reference: stdlib/write_memory](../reference/stdlib/write_memory.md)
- [Reference: state-dir](../reference/config/state-dir.md) — `memory/` location
- [events.md](events.md)
