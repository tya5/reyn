---
type: concept
topic: architecture
audience: [human, agent]
---

# Skill design patterns — three shapes most skills take

After you've built your first skill (see
[Write your first custom skill](../guide/for-skill-authors/write-your-first-custom-skill.md)),
the next question is: when I design my second one, what shape should it take?
Most Reyn skills fall into one of three patterns. Pick by what the skill needs
to do, not by complexity for its own sake.

## Pattern 1: Linear (read → process → write)

**Shape:** phases connect in a single chain with no cycles and no branches. The
LLM at each phase has exactly one allowed next phase.

```
graph:
  A: [B]
  B: [C]
  C: [end]
```

```
A --> B --> C --> end
```

**When to use:** each phase has a clear handoff to exactly one downstream
phase. The work is essentially a pipeline — gather inputs, do the main
processing, format and deliver.

**Stdlib examples:**

- `direct_llm` — the simplest case: a single phase that finishes immediately.
  Graph: `{ respond: [] }`. No preprocessor, no branching, one LLM call.
- `word_stats_demo` — single phase plus a Python preprocessor that computes
  text statistics before the LLM call. Graph: `{ review: [] }`. Demonstrates
  the deterministic split principle inside a linear shape.
- `read_local_files` — two phases. Graph:
  `{ decide_files: [read_and_respond], read_and_respond: [] }`. The LLM at
  `decide_files` issues file-read ops, then transitions to `read_and_respond`
  for the final answer.
- `skill_importer` — three-phase linear pipeline. Graph:
  `{ search: [select], select: [convert], convert: [] }`.

**Trade-off:** simple to reason about, but inflexible if the LLM produces
output the next phase can't consume. There is no recovery loop — a bad output
surfaces as a validation error and aborts.

## Pattern 2: Loop (generate → review → refine)

**Shape:** one phase in the graph has more than one allowed next phase — it can
transition forward (good enough → deliver) or back (needs work → refine again).
The cycle is declared explicitly in the graph and is first-class:

```
graph:
  generate: [review]
  review:   [generate, finalize]
  finalize: [end]
```

```
generate --> review --(needs work)--> generate
                   \--(good enough)--> finalize --> end
```

Cycles in the skill graph are not a violation — they are a deliberate feature.
See [reference/dsl/graph.md](../reference/dsl/graph.md) for the syntax.

Loops can also be realized via **OS rollback**: a phase emits
`control.type="rollback"` and the OS re-runs an earlier phase with the
feedback injected into context. This avoids adding a back-edge to the graph
while preserving the iterative behavior. Both mechanisms produce the same
observable effect from the outside.

**When to use:** output quality varies; one pass is not reliably good enough;
you want bounded retries based on judged quality (not just retries on
validation error). Common in content generation, code generation, and
iterative planning.

**Stdlib examples:**

- `skill_builder` — five-phase linear graph with an OS-rollback loop.
  Graph: `{ plan_skill: [design_artifacts], design_artifacts: [review_plan],
  review_plan: [build_skill], build_skill: [verify_skill] }`. The `verify_skill`
  phase runs `reyn lint`; if lint fails, it emits a rollback that re-runs
  `build_skill` with the lint issues as feedback. The loop is bounded by
  `max_phase_visits`.
- `skill_improver` — a longer loop skill.
  Graph: `{ prepare: [copy_to_work], copy_to_work: [run_and_eval],
  run_and_eval: [plan_improvements], plan_improvements: [apply_improvements],
  apply_improvements: [finalize] }`. `apply_improvements` rolls back to
  `run_and_eval` for the next iteration, or transitions to `finalize` when a
  stop condition is met. Loop termination conditions (score threshold, max
  iterations, regression, stagnation) are documented in the skill's `skill.md`.

**Trade-off:** powerful but cycles need a reliable finish path. Without a
termination condition the LLM can judge "needs more refinement" indefinitely.
Use `phase.max_visits`, skill-level iteration caps, or explicit stop conditions
to bound the loop.

## Pattern 3: Sub-skill composition (delegation)

**Shape:** a phase invokes another skill as a sub-skill via the `run_skill`
Control IR op. The sub-skill runs to completion and its `final_output` artifact
flows back into the parent's workspace. The parent's graph does not change
shape — but one phase's `allowed_ops` includes `run_skill`.

```
graph:
  prepare: [execute]    # execute phase issues run_skill op
  execute: [aggregate]
  aggregate: [end]
```

```
prepare --> execute --(run_skill)--> [sub-skill runs] --> execute --> aggregate --> end
```

Sub-skills can also be declared as graph nodes using the `@sub_skill` prefix.
See [Compose skills with run_skill](../guide/for-skill-authors/compose-skills-with-run-skill.md)
for both flavors.

**When to use:** the work has a self-contained sub-task that is already a
skill (or could become one), and you want to keep the parent's graph small.
Also useful when the sub-task's output needs to be validated against an
existing artifact schema before the parent proceeds.

**Stdlib examples:**

- `eval` — the `run_target` phase issues a `run_skill` Control IR op to invoke
  the target skill under test. Graph: `{ run_target: [evaluate] }`. The
  `allowed_ops: [run_skill]` declaration on `run_target` is what permits this.
  The sub-skill's output is then passed to the `evaluate` phase for judging.
- `skill_improver` — the `run_and_eval` phase invokes the `eval` skill via
  `run_skill` (documented in `skill_improver/skill.md`: "invokes the `eval`
  and `eval_builder` skills via the `run_skill` Control IR op").
- Preprocessor `run_skill` — a phase can also call a sub-skill deterministically
  before the LLM, in the preprocessor block. This is the form used by
  `recall_memory` in the chat router (see
  [Compose skills with run_skill](../guide/for-skill-authors/compose-skills-with-run-skill.md)).

**Trade-off:** composition keeps each skill's graph simple and promotes reuse
of well-tested sub-skills. The cost is a runtime dependency: if the sub-skill
doesn't exist or its contract changes, the parent breaks. Validate with
`reyn lint`.

## Mixing patterns

Real skills often combine two of the three patterns:

- **Linear + sub-skill:** a linear pipeline where one phase delegates to a
  sub-skill. `eval` is this shape — linear graph, one phase issues `run_skill`.
- **Loop + sub-skill:** a loop skill where each iteration calls a sub-skill.
  `skill_improver` is this shape — OS-rollback loop, with `run_and_eval` calling
  the `eval` sub-skill on every iteration.
- **Multi-agent (Layers 3 and 4)** is orthogonal. Any of the three patterns can
  appear inside an agent that delegates to another agent. See
  [multi-agent.md](multi-agent.md) for the broader picture.

Combining all three patterns in one skill is a warning sign — it usually means
the skill is doing too much.

## Anti-patterns to avoid

- **Over-decomposition.** Eight phases where three would do. Each phase
  boundary costs a context build. Default to fewer phases; split only when a
  phase has materially different instruction needs or a different input schema.

- **Cycle without a finish path.** The LLM judges "needs more refinement"
  indefinitely. Always include a deterministic exit condition: a
  `max_phase_visits` limit, an iteration counter checked in phase instructions,
  or an explicit stop condition that transitions to a terminal phase regardless
  of quality.

- **Sub-skill explosion.** Making everything a sub-skill so the parent graph
  "looks clean." Sub-skills add lookup overhead and create dependency surface.
  Pre-existing reusable sub-skills are cheap to compose; speculative
  future-reuse sub-skills are not.

- **Branching without judgment.** Graphs with `{ A: [B, C, D] }` where the
  LLM cannot reliably tell which branch to pick. Branching makes sense when
  the input clearly distinguishes the path (see the `skill_builder` pattern
  table for an example of well-motivated branching). Avoid branches whose
  selection criterion is too subtle for the LLM to apply reliably.

## See also

- [principles.md](principles.md) — P1 and P2: the design-time invariants
  these patterns embody (Phase declares only input; Skill owns the graph).
- [architecture.md](architecture.md) — overall component layering.
- [multi-agent.md](multi-agent.md) — Layers 3 and 4, orthogonal to these
  three patterns.
- [Write your first custom skill](../guide/for-skill-authors/write-your-first-custom-skill.md)
  — apply these patterns in practice.
- [Compose skills with run_skill](../guide/for-skill-authors/compose-skills-with-run-skill.md)
  — Pattern 3 in detail.
- [reference/dsl/graph.md](../reference/dsl/graph.md) — graph syntax and cycle
  semantics.
