---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [skill.md]
---

# `graph` semantics

The `graph` field in `skill.md` declares the allowed phase transitions. The OS uses it to validate every transition the LLM proposes.

## Structure

```yaml
graph:
  outline: [expand]
  expand:  [end]
```

Each key is a phase name. Each value is a list of allowed next-phase names. The special token `end` means "this transition terminates the workflow."

## Allowed shapes

### Linear

```yaml
graph:
  a: [b]
  b: [c]
  c: [end]
```

### Branching

```yaml
graph:
  triage:  [draft, escalate]
  draft:   [review]
  review:  [revise, end]
  revise:  [review]
  escalate: [end]
```

The LLM at `triage` may pick `draft` or `escalate`. Either choice is validated against the graph.

### Self-loops are NOT supported

A phase cannot list itself as a next phase. Revision loops use a separate phase (e.g. `review → revise → review`).

## Resolution rules

- `entry` (declared in `skill.md`) must be a key in `graph`.
- Every value-list entry must either be a key in `graph` or `end`.
- `end` may only appear in transitions from a phase whose `phases/<name>.md` has `can_finish: true`.
- Skills with sub-skill nodes (`@sub_skill` in graph) follow the same rules; the OS resolves the embedded skill at compile time.

## Sub-skills (graph nodes)

A graph entry may reference another skill by prefixing with `@`:

```yaml
graph:
  prepare:    [@my_subskill]
  '@my_subskill': [aggregate]
  aggregate:  [end]
```

`run_skill` Control IR ops use the same name resolution: `reyn/project/` → `reyn/local/` → `src/reyn/stdlib/skills/`.

## Linter checks

`reyn lint <skill_name>` enforces:

- All graph keys correspond to phase files in `phases/`.
- All graph values are either keys, sub-skill references, or `end`.
- `entry` exists.
- Phases with `can_finish: true` have a path to `end`.
- No unreachable phases.

## See also

- [skill-md.md](skill-md.md) — `entry`, `final_output`
- [phase-md.md](phase-md.md) — `can_finish`
- [Concepts: principles P2 (skill defines structure)](../../concepts/architecture/principles.md#p2-skill-defines-structure)
