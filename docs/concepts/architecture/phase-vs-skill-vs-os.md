---
type: concept
topic: architecture
audience: [human, agent]
---

# Phase vs. Skill vs. OS

reyn splits responsibilities across three layers. The split is what makes the system extensible: new skills are pure data and never require OS code changes.

## The split

| Layer | Owns | Knows about |
|-------|------|-------------|
| **OS** | Control flow, validation, events, Control IR dispatch | None of the skill's domain — just the DSL contract |
| **Skill** | The phase graph, the entry phase, the final output schema | Only its own phases and artifacts |
| **Phase** | An input artifact type and instructions for the LLM | Nothing outside its own `input` |

### Phase: stateless and reusable

A Phase declares its input and instructions. It does **not** know:

- which phase comes next
- what its output looks like
- which skill it belongs to

This is what makes phases like `revise` reusable across skills: nothing in `revise.md` couples it to a specific draft producer.

### Skill: structure-only

A Skill says "from `entry`, the graph allows these transitions, and the final output looks like this." It does not run code itself. A skill is data the OS reads.

#### Postprocessor: a skill-level finish hook

A Skill may optionally declare a `postprocessor` block — a deterministic transformation that runs at skill finish, between the LLM's final output (which conforms to the skill's `final_output` schema) and the artifact returned to the caller (which conforms to `postprocessor.output_schema`). Postprocessor mirrors the phase preprocessor structurally — same step types, same op set, same `on_error` policy — differing only in fire position (phase entry vs. skill finish). Like the skill graph, the postprocessor block is declared in `skill.md` and is invisible to both the OS's generic runtime logic and to individual phases. See [Concepts: postprocessor](../skills/postprocessor.md) and [Reference: postprocessor](../../reference/dsl/postprocessor.md).

### OS: skill-agnostic

The OS reads the skill's graph, builds the LLM context, validates the result, and dispatches Control IR. It contains zero string literals naming a specific phase, artifact, or field. When a new skill appears, OS code is untouched.

## Where each kind of change lands

| Change | Lands in |
|--------|----------|
| "Generate a different artifact field" | Phase instructions + the next phase's `input` schema |
| "Add a revision loop" | Skill's `graph` (add `review → revise → review`) |
| "Add a new control operation kind" | OS (new Control IR op) — every skill can use it for free |
| "Decide between two skills based on the user's input" | A router skill (e.g. `skill_router`); the OS does no skill picking |

## The "not in this layer" smell

When you find yourself wanting to:

- Reference a sibling phase's name from inside a Phase → wrong layer (P1). Move the connection to the Skill graph.
- Hardcode a specific artifact type in OS code → wrong layer (P7). Read the type from the skill instead.
- Embed control-flow logic in Phase instructions ("if X, go to phase Y") → wrong layer (P8). Encode the choice as `candidate_outputs` instead.

## Comparing common workflow systems

| System | Equivalent of "Skill" | Equivalent of "Phase" | Where control flow lives |
|--------|------------------------|------------------------|---------------------------|
| Imperative agents (e.g. plain prompt loop) | (none) | (the whole prompt) | LLM choices the next call freely |
| State machines | The state diagram | A state | The diagram |
| reyn | A skill folder | A phase markdown | The skill's graph + LLM picks among allowed edges |

The crucial reyn-specific point: the LLM picks the *edge* but the OS validates the choice against the graph. The LLM can't add a new edge mid-run.

## See also

- [../architecture/principles.md](../architecture/principles.md) — P1, P2, P3, P7
- [../architecture/llm-as-decision-engine.md](../architecture/llm-as-decision-engine.md)
- [Reference: skill.md](../../reference/dsl/skill-md.md)
- [Reference: phase.md](../../reference/dsl/phase-md.md)
- [Reference: graph](../../reference/dsl/graph.md)
