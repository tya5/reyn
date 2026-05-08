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

## See also

- [principles.md](principles.md) — the eight constraints
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — responsibility boundaries
- [Reference: control-ir](../reference/runtime/control-ir.md) — Control IR ops
- [Reference: events](../reference/runtime/events.md) — event types
- [Agent engineering — seven lenses](../guide/for-skill-authors/agent-engineering/index.md) — the same architecture through external engineering perspectives
