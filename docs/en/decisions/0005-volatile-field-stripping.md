# ADR-0005: Volatile field stripping for memo stability

**Status**: Accepted (2026-05-02)
**Track**: PR-llm-memo (R-D2)

## Context

The LLM memo lookup hashes the LLM call's effective input
(`(model, frame, prior_attempts, rollback_context, system_inputs)`) and
matches against recorded `args_hash` from previous successful calls.
For memo to hit on resume, the hash must be **stable** across the
crash boundary.

Auditing the inputs revealed two volatile fields that change every
call by design:

1. `ContextFrame.current_datetime` — set by `datetime.now().astimezone()`
   each time the frame is built. Provides the LLM with current time.
2. `ContextFrame.execution.path` — derived from `OSRuntime._history`,
   which is restored from `snap.history` on resume. The two formats
   differ: `_history` records transition strings ("draft → review")
   in normal operation, but the snapshot stores phase names
   ("draft"). Restoring as-is corrupts the format.

Without intervention, every resume hashes differently from the
recorded value → memo always misses → cost duplication.

## Considered alternatives

- **A. Freeze datetime on resume.** Rejected: not actually volatile
  in concept; legitimately changes between original run and resume.
  Forcing a frozen value would make the LLM see "it's still
  yesterday".
- **B. Record-and-replay datetime.** Cleanest semantically: store the
  `current_datetime` value in the recorded step, inject it back into
  the frame on resume. LLM sees bit-perfect original prompt. But
  requires plumbing through ContextFrame builder + per-step
  recording. Significant scope addition.
- **C. Strip volatile fields from the hash.** Adopted. The LLM still
  receives fresh datetime in its frame, but the memo key is computed
  over a normalized frame with the volatile field removed.

For `execution.path` the choice between B and C is forced by an
underlying schema mismatch:

- **B for path**: would require adding `transition_history` field to
  `SkillSnapshot` + threading the from/to phase pair through
  advance_phase. Tracked as R-D11.
- **C for path**: same strip approach as datetime. Trivially small.

## Decision

**Adopt C: strip volatile fields from the args_hash.**

`_LLM_VOLATILE_FRAME_FIELDS` (top-level fields excluded from hash):
- `current_datetime`

`_LLM_VOLATILE_NESTED_FIELDS` (nested fields, "<top>.<sub>" syntax):
- `execution.path`

`_compute_llm_args_hash` builds a canonical frame with these fields
removed before serializing to JSON and hashing.

Memo hit semantics: when memo hits, the LLM is NOT called → it never
sees the resumed-time datetime; the recorded response is returned
directly. Memo miss (= drift detected) → fresh LLM call sees the
current datetime, which is correct behavior.

## Consequences

**Positive:**

- Memo stability across resume — happy-path skill resumes hit memo
  reliably.
- No schema additions required for either issue.
- LLM still sees correct fresh datetime when actually called.

**Negative:**

- Drift detection on these fields is lost. If something IS materially
  different about the path or datetime, the memo doesn't catch it.
  Acceptable: path is informational (display only), and datetime
  drift between runs is expected, not pathological.
- `execution.path` shown to the LLM during resume (when fresh-called)
  may be format-inconsistent (mix of phase names from snapshot +
  transition strings from new transitions). This affects only the
  LLM's view of "where we've been" — informational, not decision-
  critical. Full fix tracked as R-D11.

**Precluded:**

- Bit-perfect prompt re-creation across resume. If audit-trail
  bit-perfectness becomes a hard requirement (e.g. for replay-based
  determinism testing), revisit B (record-and-replay) — its
  groundwork is intentionally left as a follow-up option.

## References

- Commit `6c2d9dc` — R-D2 L1 `_compute_llm_args_hash`
- Commit `ffcbeb4` — R-D2 L3 `execution.path` exclusion fix
- R-D11 (transition_history field in SkillSnapshot, post-resume-ux)
- ADR-0004 (memoization key — what this hash is for)
