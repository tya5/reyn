---
type: concept
topic: architecture
audience: [human, agent]
---

# System Design

The macro shape of an agent system: how control flow, state, and responsibility are distributed across layers, and what invariants the runtime enforces no matter what the LLM does.

## How reyn handles it

Three layers, each with a single responsibility:

| Layer | Owns | Knows about |
|-------|------|-------------|
| **OS** | The runtime loop, validation, events | None of the workflow's domain |
| **Workflow** | The graph, entry phase, final output schema | Only its own phases and artifacts |
| **Phase** | An input artifact type + LLM instructions | Nothing outside its own input |

Two invariants follow from the split:

1. **The graph constrains the LLM.** The LLM picks an edge from `candidate_outputs`; the OS rejects anything else. Control flow is a finite state machine, not free-form prompt chaining.
2. **The OS is workflow-agnostic.** No string literal naming a specific phase, artifact, or field appears in OS code (P7). New workflows are pure data; OS code never changes.

The result: a workflow's behavior is a function of its workflow files plus the LLM's choices within each phase — both small, both inspectable.

### Why bound the LLM's control flow

Unbounded LLM orchestration is unstable in three measurable ways:

- **Drift.** Each free choice is a chance to wander off-task.
- **Untestability.** "Will this prompt eventually finish?" is undecidable for a free agent and trivially decidable on a finite graph.
- **No clean re-entry.** When something fails, you want to point to the failing phase. Free-form orchestration has no phases to point at.

reyn pays the cost of writing workflow graphs explicitly and gets predictability in return.

## Where it's still thin

The graph is a DAG with no self-loops — a phase cannot list itself as a next phase. Revision loops use a separate phase (`review → revise → review`). This is fine in practice but adds one node where some frameworks let you "retry the same step inline." Sub-run nodes (`@subskill` in the graph) are the escape hatch when a single phase isn't enough.

## See also

- [../architecture/llm-as-decision-engine.md](../architecture/llm-as-decision-engine.md)
- [tool-contract-design.md](tool-contract-design.md) — how the LLM expresses its choices
- [reliability-engineering.md](reliability-engineering.md) — what happens when the LLM gets it wrong
