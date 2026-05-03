# ADR-0008: Intervention answer in-memory buffering (MVP)

**Status**: Accepted (2026-05-03)
**Track**: PR-intervention-link L6 + L7

## Context

`ask_user` interventions (and permission prompts) block a skill's
execution until the user answers. PR-intervention-link L1–L5 made the
intervention itself crash-recoverable: on restart, the snapshot's
`outstanding_interventions` is restored to the registry queue, and the
user can answer via `/answer`.

But what happens to that answer? In the original (non-resume) path,
the dispatch coroutine awaits `iv.future` and returns the answer to
the skill via `bus.request`. After a crash that coroutine is dead.
The skill on resume calls `bus.request` again — should it create a
fresh intervention (= duplicate prompt for the user), or pick up the
restored intervention's answer somehow?

Three full-correctness states need handling:

1. **User answers BEFORE skill resumes.** Answer must be captured for
   the resuming skill to consume.
2. **User answers AFTER skill resumes.** The resuming skill is already
   blocked on a fresh `bus.request`; user answers that fresh
   intervention normally.
3. **User answers, then process crashes BEFORE skill consumes the
   answer.** State 1 with a second crash. Answer is in flight but
   unrecorded.

State 3 is the durability question. State 1 is the common case.

## Considered alternatives

- **A. WAL-durable answer buffering.** Add `intervention_answered` and
  `intervention_consumed` WAL events; the answer payload lives in the
  snapshot until consumed. Handles all three states correctly. But:
  - Requires new WAL kinds + AgentSnapshot field
  - Changes the semantics of `intervention_resolved` (consumption vs
    answer)
  - Significant scope for L6 — risks PR bloat
- **B. In-memory buffer keyed by run_id.** When a restored
  intervention resolves, capture the answer in
  `ChatSession._buffered_intervention_answers`. Resuming skill's
  `bus.request` consumes from buffer first. Handles states 1 and 2.
  State 3 fails: if the process crashes after user answer but before
  skill consumption, the answer is lost (the user must re-trigger).
- **C. Mutate the iv to delivered state, let resume pick up via new
  intervention.** Confusing (two ivs in flight for same run); rejected.

## Decision

**Adopt B for now, track A as R-D12 follow-up.**

Implementation:

- `ChatSession._buffered_intervention_answers: dict[run_id, InterventionAnswer]`
  populated by the L5 watcher when a restored intervention resolves.
- `ChatInterventionBus.request` short-circuits if a buffer entry
  exists for `iv.run_id` — returns the buffered answer without
  dispatching a new intervention.
- Buffer is single-use: consumed on first lookup, popped from the
  dict.
- Drop on cancellation: `_drop_interventions_for_run` clears the
  buffer for that run.
- State 3 race documented in the commit message and ADR.

## Consequences

**Positive:**

- Implementation is small (~30 lines) and isolated to ChatSession +
  ChatInterventionBus.
- States 1 and 2 — the common scenarios — work correctly.
- Existing `intervention_resolved` semantics unchanged; no schema
  changes needed.
- L6 ships fast; the PR-intervention-link series stays tractable.

**Negative:**

- State 3 race: user answers, process crashes, answer lost. User
  must re-trigger the skill (= the snapshot's
  outstanding_interventions was already pruned by the resolved event,
  so the iv won't re-appear in the queue either). Edge case in
  practice — typical "restart → answer → resume" happens in one
  process lifetime — but real.
- Future durability work (R-D12) will need to refactor the
  intervention_resolved semantics, which means breaking changes
  across this layer.

**Precluded:**

- Production-grade durability for interventions during the
  PR-intervention-link era. Acknowledged and tracked.

## References

- Commit `7d1035f` — L6 + L7 implementation
- R-D12 (durable answer buffering, future PR)
- ADR-0007 (resume prompt UX — uses this buffer)
