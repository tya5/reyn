---
type: concept
topic: architecture
audience: [human, agent]
---

# Workspace

The workspace is the single source of truth for everything reyn produces during a run: artifacts, intermediate files, tool outputs, and the event log. Phases never communicate via in-memory side-channels — if a Phase wants to share something with a later phase, it goes through the workspace.

## What lives in the workspace

| Kind | Where |
|------|-------|
| The current artifact (input → next phase) | In-memory between phases, persisted at transitions |
| Files written by `file.write` Control IR ops | Under the workspace root the skill chose |
| Sub-skill outputs (from `run_skill` ops) | Bound to a named slot in the calling phase's input |
| Event log | `.reyn/events/<run_id>.jsonl` |
| Eval reports | `.reyn/eval_reports/<skill>/<timestamp>.json` |

## Why a single source

Two consequences fall out of having one workspace:

- **Replayability.** Because every write goes through the OS and emits an event, the event log alone is enough to reconstruct what the workflow saw. There is no "hidden state" the OS could be missing.
- **Determinism boundaries.** When something goes wrong, you can ask "did this Phase see the right input?" with a single `cat` of the workspace. There is no second source to reconcile against.

## Phase-to-phase data flow

A Phase produces an artifact. The OS:

1. Validates the artifact against the next target's schema.
2. Stores it as the input for the next phase visit.
3. Optionally executes Control IR ops (file writes, sub-skill calls), which may mutate the workspace.

Phases never directly hand data to each other. The chain is always Phase → OS → Phase.

## Files vs. artifacts

A file lives on disk; an artifact lives in the OS's typed channel between phases. Both are "workspace state," but only the artifact participates in transition validation.

Use files when:

- the data is large or naturally a file (a generated report, a transcript)
- multiple phases each pick out different parts
- the user will read it after the run

Use artifacts when:

- the data is structured and a downstream phase needs to validate it
- the data flows along a single edge of the graph

## See also

- [principles.md](principles.md) — P3 (OS controls execution), P5 (Workspace is the single source of truth)
- [Reference: artifact.yaml](../reference/dsl/artifact-yaml.md)
- [Reference: control-ir](../reference/runtime/control-ir.md)
- [events.md](events.md)
