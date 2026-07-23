---
type: concept
topic: architecture
audience: [human, agent]
---

# Workspace

The workspace is the single source of truth for everything reyn produces during a run: intermediate files, tool outputs, and the event log. Every write goes through the OS and emits an event.

## What lives in the workspace

| Kind | Where |
|------|-------|
| Files written by `file.write` Control IR ops | Under the workspace root the agent chose |
| Event log | `.reyn/events/<run_id>.jsonl` |
| Eval reports | `.reyn/eval-results/<skill>/<timestamp>.json` |

## Why a single source

Two consequences fall out of having one workspace:

- **Replayability.** Because every write goes through the OS and emits an event, the event log alone is enough to reconstruct what the workflow saw. There is no "hidden state" the OS could be missing.
- **Determinism boundaries.** When something goes wrong, you can ask "did the run see the right input?" with a single `cat` of the workspace. There is no second source to reconcile against.

## See also

- [Reference: control-ir](../../reference/runtime/control-ir.md)
- [../runtime/events.md](../runtime/events.md)
