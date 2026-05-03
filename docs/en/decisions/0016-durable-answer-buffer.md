# ADR-0016: Durable intervention answer buffer

**Status**: Accepted (2026-05-04). Supersedes [ADR-0008](0008-intervention-answer-buffering.md).
**Track**: R-D12 (commit `01c29b7`)

## Context

[ADR-0008](0008-intervention-answer-buffering.md) shipped an in-memory
buffer for intervention answers received before the resuming skill
consumed them. It explicitly accepted state 3 — user answers, then
process crashes before the skill consumes the answer — as a tracked
gap, with R-D12 to follow up.

The state 3 race becomes plausible enough to fix:

- A user is woken by an `ask_user` prompt, types a quick answer.
- The Reyn process crashes (network blip, OS upgrade reboot, OOM)
  before the resuming skill picks the answer off the buffer.
- On next start, the snapshot's `outstanding_interventions` already
  reflects the resolved state (the iv was removed when the answer
  arrived). The buffer was in-memory only — gone.
- The user's answer is silently lost; the skill resumes, hits a fresh
  `ask_user`, asks the same question again. Frustrating and confusing.

ADR-0008 was a deliberate MVP step to ship PR-intervention-link L6;
R-D12 was always planned to close the gap once the surrounding
machinery was stable.

## Considered alternatives

- **A. WAL-durable buffer with new event kinds.** Add
  `intervention_answer_buffered` and `intervention_answer_consumed`
  WAL events; persist the answer payload on the AgentSnapshot until
  the skill consumes it. Survives a second crash.
- **B. Reuse `intervention_dispatched` / `intervention_resolved` and
  embed answer in the resolved event.** Tighter event taxonomy but
  conflates the "user answered" and "skill consumed" semantics, which
  is exactly what state 3 needs to distinguish. Rejected.
- **C. Disk-backed buffer file (no WAL).** A second source of truth
  for intervention state; fragile and breaks the WAL-as-truth
  invariant from [ADR-0001](0001-state-model-wal-snapshot.md).

## Decision

**Adopt A.**

- Two new WAL kinds in `src/reyn/events/state_log.py`:
  `intervention_answer_buffered` and `intervention_answer_consumed`.
- `AgentSnapshot.buffered_intervention_answers: dict[str, dict]`
  field: keyed by run_id, value carries the answer payload + metadata.
- `apply_events` handles the new kinds: `_buffered` writes the entry,
  `_consumed` deletes it.
- `SnapshotJournal` exposes `record_intervention_answer_buffered` and
  `record_intervention_answer_consumed`.
- `ChatSession.restore_state` rehydrates the buffer dict from the
  snapshot, so state 3 (process crashed mid-buffer) restores cleanly.
- `ChatInterventionBus.request` consults the buffer first as before,
  but consumption now emits the `_consumed` event.

State analysis:

| Crash timing | Outcome |
|---|---|
| Before user answers | iv stays in `outstanding_interventions`; resume re-asks (= state 1 from ADR-0008, unchanged) |
| After user answers, before skill resumes | answer recorded as `_buffered`, snapshot persists it; resume picks it up (was state 1 in ADR-0008, now durable) |
| After skill resumes consumes answer | `_consumed` event recorded; nothing to restore (was state 2 in ADR-0008, unchanged) |
| After answer arrives, before skill consumes (= state 3) | `_buffered` event made the answer durable; resume restores it and skill consumes on next request |

## Consequences

**Positive:**

- All three states from ADR-0008 are now correct. The user's typed
  answer is honoured even across a second crash.
- Schema changes are localised: two new event kinds, one snapshot
  field. AgentSnapshot replay handlers cover the new kinds with the
  same `apply_events` pattern as the rest.
- WAL stays the single source of truth (no parallel disk file).

**Negative:**

- Answer payloads land in the WAL. For typical text answers (a few
  hundred bytes) this is fine; multi-KB answers (rare in practice) add
  to the WAL's per-phase weight. Off-loading is not implemented for
  intervention payloads (could mirror [ADR-0015](0015-llm-result-workspace-ref.md)
  if needed).
- Two new WAL kinds to maintain; the pre-1.0 schema bump is bundled
  into the same release as the other R-D items.

**Precluded:**

- Reverting to in-memory buffer for performance reasons. The audit
  trail value of the `_buffered` / `_consumed` pair plus state 3
  correctness outweigh the (negligible) WAL append cost.

## References

- Commit `01c29b7` — implementation + Tier 2 tests
  (`test_durable_answer_buffer.py`)
- [ADR-0008](0008-intervention-answer-buffering.md) — the MVP buffer
  this supersedes
- [ADR-0001](0001-state-model-wal-snapshot.md) — WAL-as-truth invariant
