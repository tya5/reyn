# ADR-0009: Pre-decrement visit_count on resume

**Status**: Accepted (2026-05-03)
**Track**: PR-llm-memo (R-D2) — surfaced during e2e

## Context

R-D2 (LLM call memoization) requires the args_hash computed on resume
to match the args_hash recorded during the original run. The hash is
computed over `(model, frame, ...)` where `frame` is the
`ContextFrame` for that LLM call. The frame includes
`execution.current_visit` (= the current phase's visit count) and
`execution.total_steps` (= sum of all visit_counts).

During e2e testing, every memo lookup missed even though everything
else looked correct. Tracing showed: in the original run, the first
LLM call in phase X had `visit_counts = {X: 1}`. On resume, after
restoring `_visit_counts` from the snapshot, `_enter_phase(X)`
incremented the counter — so the resumed first LLM call saw
`visit_counts = {X: 2}`.

The frame differed → args_hash differed → memo always missed.

## Considered alternatives

- **A. Strip `current_visit` and `total_steps` from the args_hash.**
  Rejected: these are real LLM-visible context. Stripping them means
  the LLM's view of execution state isn't part of the memo key, which
  loses drift detection on a meaningful axis.
- **B. Store `_visit_counts` AT crash time (before increment).**
  Rejected: would require capturing pre-increment state in the
  snapshot, which conflicts with `advance_phase`'s natural ordering
  (advance_phase records the post-increment state).
- **C. Skip `_enter_phase` for the resumed phase entirely.** Rejected:
  `_enter_phase` does more than visit_count update (resets timer,
  emits `phase_started` event); skipping it would create silent
  divergences elsewhere.
- **D. Pre-decrement visit_count for the resumed phase BEFORE
  `_enter_phase` runs.** Adopted. The increment lands on the same
  count the original run had.

## Decision

**Adopt D: pre-decrement visit_count on resume.**

In `OSRuntime.run`'s resume restoration block:

```python
self._visit_counts = dict(self._resume_plan.visit_counts)
# Pre-decrement visit_count for the resumed phase so that the
# upcoming `_enter_phase` increment lands on the SAME count the
# original run had at the time the LLM was called. Without this,
# the in-flight phase's first LLM call sees visit_count = recorded
# + 1, the args_hash differs from what was recorded, and memo
# lookup misses every time (silent cost duplication).
if current_phase in self._visit_counts and \
        self._visit_counts[current_phase] > 0:
    self._visit_counts[current_phase] -= 1
```

The decrement applies only to the resumed phase. Other phases'
counts are restored as-is.

## Consequences

**Positive:**

- Memo args_hash stable across crash boundary for the resumed
  phase's first LLM call.
- `_enter_phase` semantics preserved — full event emission, timer
  reset, etc.
- Localized fix: 4 lines of code, no schema change.

**Negative:**

- Subtle: a code reader sees a "decrement" inside resume code and
  asks why. Mitigated by an explicit comment quoting the failure
  mode.
- Same off-by-one symptom could surface for any other phase-scoped
  state that's incremented on `_enter_phase`. Currently only
  visit_counts has this; if another counter is added without the
  same treatment, it'll miss memo silently. Test coverage in
  `test_llm_memoization_e2e.py` catches the LLM symptom.

**Precluded:**

- A cleaner refactor that splits `_enter_phase` into "first-time
  entry" vs "re-entry" paths. Would be more correct architecturally
  but is much larger scope. Tracked as a future polish opportunity
  (not yet a tracked R-D).

## References

- Commit `ffcbeb4` — R-D2 L3 e2e + visit_count fix
- ADR-0004 (memoization key — what consumes the args_hash)
- ADR-0005 (volatile field stripping — sibling fix that surfaced
  together)
