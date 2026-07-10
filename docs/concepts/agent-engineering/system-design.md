---
type: concept
topic: architecture
audience: [human, agent]
---

# System Design

The macro shape of an agent system: how control flow, state, and responsibility are distributed across layers, and what invariants the runtime enforces no matter what the LLM does.

## How reyn handles it

### The layer split: LLM decides, OS executes, feature owns its own domain

reyn's current shape has no fixed graph of phases to walk. Instead, three responsibilities are kept structurally separate:

| Layer | Owns | Knows about |
|-------|------|-------------|
| **LLM** | Which action to take next, in what order, with what arguments | Whatever the current turn's context frame contains — nothing else |
| **OS** | Op dispatch, schema validation, the permission gate, the audit-event log, the workspace | No workflow/skill/pipeline-specific string literal (the still-true P7 invariant) |
| **Feature (skill / pipeline / agent)** | Its own instructions, its own artifacts, its own scope | Nothing outside what it's given — a skill can't reach into another skill's state |

Two invariants hold regardless of which surface the LLM is talking through (chat router, A2A, MCP):

1. **Every side effect is a typed, schema-validated Control IR op — never a free-form string the LLM constructs by hand.** The OS rejects malformed ops before they run; there is no path from LLM output straight to a side effect without passing through op-schema validation.
2. **Every op routes through the same exclude → permission → dispatch gate, regardless of which tool-use scheme presents it to the LLM** (native function-calling, universal action catalog wrapper, CodeAct, …). The presentation layer is swappable; the gate underneath it is not.

### Where hard, structural boundedness still exists: Pipeline

The old phase-graph engine's safety argument was a finite-state-machine: the LLM could only pick an edge from a closed candidate set, so the whole run was provably bounded. That specific mechanism is gone, but the same *kind* of guarantee — a closed, non-Turing-complete control-plane the LLM cannot escape — still exists today for the case that wants it: **Pipeline**. A pipeline is a deterministic DSL whose composition primitives are structurally closed (no nested `launch`, no arbitrary recursion), so safety and crash-recovery come from the DSL's shape, not from a runtime policy layered on top of an unbounded execution graph.

Chat (the router loop) deliberately does **not** re-impose that same hard boundedness — its safety argument is different: typed-op validation + the permission gate + bounded-loop-with-force-close + the audit-event/WAL trail, rather than a closed graph the LLM can't step outside of. Choosing Pipeline vs. chat-router orchestration for a given task is choosing which of these two safety arguments you want.

### Concrete current mechanisms

- **Chat session / router loop** — a `RouterLoop` dispatches each LLM-chosen action (skill run, agent delegation, pipeline launch, MCP call, memory op, …) through the same permission-gated dispatch path.
- **Skills** — layered-disclosure instructions (an L1 system-prompt menu → L2 on-demand full read → L3 bundled-asset read), not programs the OS executes; the model chooses to read them.
- **Environment backend abstraction** — `EnvironmentBackend` abstracts *where* repo-FS read/write/exec happens (host vs. container) away from the OS + permission layer entirely, so the governance layer doesn't change based on execution location.
- **Workspace (P5) as the single source of truth** — every artifact an agent produces passes through the workspace channel; nothing load-bearing lives only in an LLM's context window.
- **The P6 audit-event log** — every state change the OS causes emits an audit-event, giving every run a complete, replayable record independent of what the LLM chose to do.

## Where it's still thin

The trade-off from dropping the phase-graph's hard finite-state-machine is real, not just cosmetic: chat-router orchestration no longer gives a *structural* guarantee that a given run terminates or stays on a fixed set of paths — that guarantee now lives in bounded-loop-with-force-close (a runtime policy) rather than in the shape of a closed graph (a structural property the LLM literally cannot violate). Pipeline is the escape hatch back to a structural guarantee when a task needs one; chat orchestration is the more open-ended default. Whether that trade is worth it is a per-task judgment call, not a settled question — see [reliability-engineering.md](reliability-engineering.md) for how the current bounded-loop mechanism works.

## See also

- `CLAUDE.md` (§ Constitution) — the System Design lens's pass-line, canonical
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — the System Design row grounded across all 7 feature families
- [tool-contract-design.md](tool-contract-design.md) — how the LLM expresses its choices (the typed op contract this page's invariant #1 depends on)
- [reliability-engineering.md](reliability-engineering.md) — what happens when the LLM gets it wrong, and how bounded-loop-with-force-close works
- [`docs/concepts/runtime/pipelines.md`](../runtime/pipelines.md) — Pipeline's structural closedness in full
