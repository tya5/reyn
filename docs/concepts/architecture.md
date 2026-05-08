---
type: concept
topic: architecture
audience: [human, agent]
---

# Architecture overview

```
User → Agent → Skill → OS → Phase → Workspace
                  ↘ Event (record everything)
```

## Layers

### Agent

Interprets user intent. Selects or generates a Skill. Does NOT execute phases.

In practice the "Agent" today is the CLI plus chat router — both are thin and route the user's input to a Skill.

### Skill

A directory of markdown + YAML files. Defines the phase graph and the final output schema. Does not contain executable code (except optional Python preprocessor steps, which are sandboxed).

### Phase

A reusable processing unit. Declares only its `input` and instructions.

### OS

The runtime executor. Sole owner of control flow. See [principles.md](principles.md) P3 and P7.

### Workspace

The single source of truth for data. All files, tool outputs, and artifacts live here. Phases read/write via Control IR.

### Artifact

Structured data passed between phases. Validated against schemas declared in `artifacts/*.yaml`.

### Event

Every state change emits an event. Replayable for debugging and (eventually) checkpointing.

## The runtime loop

For each phase visit:

1. Build the context frame (instructions + input + candidate outputs + control ops).
2. Run preprocessor steps if any (deterministic — `reference/dsl/preprocessor.md`, Phase 2).
3. Call the LLM.
4. Receive: `next_phase | finish`, an artifact, optional Control IR ops.
5. Validate the output against OS rules and against the chosen target's schema.
6. Execute Control IR ops (file ops, ask_user, sub-skills, etc.).
7. Update workspace.
8. Emit events.
9. Transition or terminate.

## Why this shape?

Three properties fall out of the layering:

- **Replayability.** Because every state change is an event and the OS is the only mutator, a saved event log replays the same workflow deterministically (modulo the LLM call itself).
- **Skill portability.** Because the OS knows nothing about specific skills (P7), adding a new skill never changes OS code. Skills are pure data + LLM-readable instructions.
- **Bounded LLM creativity.** Because the LLM picks from a fixed set of OS-provided transitions (P4), it can't invent control flow that breaks invariants.

## Reyn through the act-sense-react lens

The broader agent community has converged on a working definition of what makes
a system an "agent": it must have the ability to affect the world, sense how it
affected the world, and choose to make additional actions — forming a closed
**act → sense → re-act feedback loop**. This framing was articulated prominently
in Tines's post ["What, exactly, is an 'AI Agent'? Here's a litmus
test"](https://www.tines.com/blog/a-litmus-test-for-ai-agents/) and the
accompanying HN discussion, where multiple commenters independently converged on
the loop formulation as the minimum requirement for agency.

Reyn implements this loop structurally, not nominally. Every step of the loop
maps to a concrete primitive:

| Loop step | Reyn primitive |
|-----------|----------------|
| **act** | Phase outputs `control_ir` — the LLM's decision, dispatched by the OS |
| **sense** | Workspace and Events, read by the next phase's context frame |
| **re-act** | LLM produces the next transition and artifact in the new context |
| **loop closure** | Skill graph `transitions` and finish condition |

The structural nature of this mapping is what distinguishes Reyn from frameworks
where the loop is implicit. In many agent systems, "sensing" is whatever the LLM
happens to read, "acting" is whatever tool it happens to call, and the loop
closes only because the LLM decides to keep going. Reyn makes each step
explicit and OS-owned:

- Workspace is the only sensing channel — what the LLM sees is exactly what the
  OS built into the context frame, no more.
- Events are the only audit record — every sense-act cycle leaves a replayable
  trace ([events.md](events.md)).
- Control IR is the only acting vocabulary — the LLM cannot invent new
  operations outside the declared op set.
- The Skill graph is the only re-act path — the LLM picks among OS-validated
  transitions; it cannot add a new edge mid-run ([principles.md](principles.md#p3-os-controls-execution)).

This is what [P3 (OS controls execution)](principles.md#p3-os-controls-execution)
makes concrete in the loop framing: the OS owns the loop structure; the LLM
makes decisions inside it.

For readers familiar with other agent frameworks — LangGraph, AutoGen, Semantic
Kernel — this mapping provides a direct correspondence. Where those systems
expose the loop as a programmable surface, Reyn encodes it as a validated
runtime contract. The LLM's role is the same in all cases (deciding the next
step); what differs is whether the loop boundary is enforced by code or by
convention.

## See also

- [principles.md](principles.md) — the eight constraints
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — responsibility boundaries
- [workspace.md](workspace.md) — Workspace in depth
- [events.md](events.md) — Events in depth
- [Reference: control-ir](../reference/runtime/control-ir.md) — Control IR ops
- [Reference: events](../reference/runtime/events.md) — event types
- [Agent engineering — seven lenses](agent-engineering/index.md) — the same architecture through external engineering perspectives
