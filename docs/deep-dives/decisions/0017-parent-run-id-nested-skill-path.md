# ADR-0017: parent_run_id for nested skill path display

**Status**: Accepted (2026-05-04)
**Track**: R-D13 (commit `16b0e57`)

## Context

A skill invoked via `run_skill` from inside another skill's phase
spawns a child run with its own `run_id`. The
`SkillResumeCoordinator` and `/skill list` slash both treat each
run_id as a flat entry, so a parent → child relationship looks like
two unrelated lines:

```
alpha / blog_writer  …
alpha / draft_review …
```

even though `draft_review` is the child of `blog_writer`. For
debugging and operator awareness, the parent / child lineage is
useful. ADR-0007 explicitly deferred this as "not in the bulk prompt;
revisit in R-D13".

[ADR-0012](0012-auto-resume-default.md) removed the bulk prompt
itself, but `/skill list` still benefits from the lineage display.
The nested context is also useful for the future
PR-discard-cascade-reissue work, which needs to reason about parent /
child relationships when discarding.

## Considered alternatives

- **A. Reconstruct lineage by scanning WAL events.** Parent's
  `step_completed` for the `run_skill` op carries the child run_id;
  in principle one can walk back from a child to its parent by event
  scan. Cost: scanning the WAL on every `/skill list` invocation.
  Reliability: works only if events are still in WAL (not truncated).
- **B. Persist `parent_run_id` on the child's snapshot.** A static
  forward link from child → parent. O(1) lookup. Survives WAL
  truncation. Snapshot field, no event scan.
- **C. Persist a tree (parent → children list).** Symmetric but
  imposes mutation on parent's snapshot whenever a child spawns,
  which complicates the skill registry's atomicity model.

## Decision

**Adopt B.** Six-layer plumbing:

1. `SkillSnapshot.parent_run_id: str | None = None` — new field with
   `save` / `load` support.
2. `SkillRegistry.start(...)` accepts `parent_run_id` parameter and
   stores it in the snapshot.
3. `Agent.run(...)` accepts `parent_run_id` and threads it to
   `SkillRegistry.start`.
4. `op_runtime/run_skill.py` passes `parent_run_id=ctx.parent_skill_run_id`
   to `invoke_sub_skill`.
5. `op_runtime/context.py` adds `parent_skill_run_id: str | None = None`
   field on `OpContext`.
6. `kernel/control_ir_executor.py._build_ctx` populates the new field
   from `self._skill_run_id`, so a child running inside a parent's
   phase sees the parent's run_id in its `OpContext`.

Display side: `chat/slash/skill.py._list_skill_runs` walks
`parent_run_id` chain to render `parent_skill / child_skill` lineage:

```
alpha / blog_writer
alpha / blog_writer / draft_review
```

`parent_run_id = None` means root skill (the default for
backward-compatibility with existing snapshots).

## Consequences

**Positive:**

- Lineage display is O(1) per run_id; no WAL scan.
- Survives WAL truncation by living on the snapshot.
- Backward-compatible: existing snapshots without the field load as
  root skills.
- The new context field unlocks future cross-skill audit
  (cascade-discard, parent-bound budget aggregation, etc.) without
  needing a separate migration.

**Negative:**

- Six layers of plumbing for one field. The cost is paid once;
  subsequent extensions can reuse the conduit.
- A child whose parent has already completed is now an "orphaned"
  reference (`parent_run_id` points to a deleted snapshot). Display
  layer handles this by treating an unresolvable parent as "root";
  no error.

**Precluded:**

- Symmetric tree on the parent. If a feature needs "all my children",
  it can derive by scanning child snapshots, or this ADR can be
  superseded later.

## References

- Commit `16b0e57` — implementation + Tier 2 tests
  (`test_nested_skill_path.py`)
- [ADR-0007](0007-bulk-resume-prompt-ux.md) — original deferral as
  R-D13 follow-up
- [ADR-0012](0012-auto-resume-default.md) — `/skill list` is the
  primary consumer of the lineage display
- Future PR-discard-cascade-reissue — the planned consumer of the
  parent-bound context field
