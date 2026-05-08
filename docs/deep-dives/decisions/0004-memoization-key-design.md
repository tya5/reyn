# ADR-0004: Memoization key — (op_invocation_id, phase, args_hash)

**Status**: Accepted (2026-05-02)
**Track**: D-track (D3b-1), PR-llm-memo (R-D2)

## Context

Resume re-enters the in-flight phase from start (ADR-0002). Within that
phase, ops and LLM calls that already completed before the crash should
NOT re-execute — that's the whole point. The resume runtime needs a
**memoization key** to look up "did this specific call already happen,
and what was its result?"

The key must satisfy:

1. Uniquely identify each call within a skill_run
2. Survive the crash (i.e., be derivable from WAL contents)
3. Detect drift (= the same call attempted with different inputs after
   a code change or environment shift) so resume falls through to a
   fresh execution
4. Be cheap to compute and compare

## Considered alternatives

- **A. Sequence number alone (`step_seq`).** Rejected: WAL seq is
  global across agents. Resume of skill X re-walks its phases and
  emits new step events that get fresh seqs; matching is impossible
  without a seq-replay map.
- **B. `args_hash` alone.** Rejected: same op invoked twice in one
  phase with the same args (idempotent retries) gives the same hash;
  can't distinguish them.
- **C. (op_kind, args_hash).** Rejected: still can't distinguish
  multiple identical-args invocations in the same phase visit.
- **D. `(op_invocation_id, phase, args_hash)` triple.** Adopted.
  - `op_invocation_id` = `{phase}.{op_idx}` (or `{phase}.llm.{idx}`
    for LLM). Phase-local sequential counter, deterministic on
    re-entry.
  - `phase` = phase name; matches the phase visit context.
  - `args_hash` = SHA-256 of canonicalized args (truncated 16 hex).
    Detects drift.

## Decision

**Adopt D: triple of (op_invocation_id, phase, args_hash).**

Implementation in `dispatch_tool` (`dispatcher.py:_lookup_memoized_step`):

```python
for step in resume_plan.committed_steps:
    if (step.op_invocation_id == op_invocation_id
            and step.phase == phase
            and step.args_hash == args_hash):
        # last-write-wins on duplicates (handles botched truncation)
        if best is None or step.seq > best.seq:
            best = step
return best
```

Last-write-wins handles the edge case where a botched WAL truncation
left two completions for the same step.

For LLM calls the same pattern applies: `op_invocation_id =
"{phase}.llm.{act_turn_count}"` where `act_turn_count` is reset by
`_enter_phase` (so resume's first LLM call always looks up
`{phase}.llm.0` matching the recorded entry).

## Consequences

**Positive:**

- Correctness by construction: same call = same key, different call =
  different key.
- Drift detection: a code change that alters op args (different file
  path, different prompt) computes a different hash → memo miss → op
  re-executes (safe).
- Phase-scoped: avoids cross-phase collisions.
- Format uniform across op-kind and llm-kind steps (16 hex chars), so
  audit tooling has a stable shape.

**Negative:**

- Volatile fields in the args (e.g. `current_datetime` in LLM frame)
  must be stripped before hashing or every resume produces a hash
  mismatch and silent memo miss. Solved by ADR-0005.
- Phase retry / rollback paths can produce duplicate `op_invocation_id`
  values (different retry, same act_turn_count). Today this is
  handled defensively by `prior_attempts` differing across retries
  (so args_hash differs naturally), but the structural fix is tracked
  as R-D11.

**Precluded:**

- Cannot memoize across phases (op result of phase A consumed in phase
  B). This was never a goal — phases are isolated by P5 (workspace as
  SSoT), and cross-phase data flows via workspace artifacts.

## References

- Commit `26859a3` — D3b-1 dispatch_tool memoization
- Commit `c511422` — R-D2 LLM call memoization in
  `_call_llm_and_record`
- ADR-0005 (volatile field stripping)
- ADR-0009 (visit_count off-by-one fix that surfaced during this work)
- R-D11 (op_invocation_id collision audit follow-up)
