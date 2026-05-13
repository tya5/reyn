# ADR-0027c: seal_unit and plan-mode integration for AuditSeal

**Status**: Proposed
**Date**: 2026-05-13
**Depends on**: ADR-0027 (AuditSeal Separation), ADR-0023 (Plan-Mode Forward Replay)

---

## Context

ADR-0027 defines `seal_unit: skill` as the default, meaning one `AuditSeal`
is produced per skill run completion. The configuration allows `seal_unit: phase`
as a future extension.

ADR-0023 (Plan-Mode Forward Replay) establishes that a single plan execution
(triggered by the `plan` router tool) can **spawn multiple concurrent skill
runs** via `PlanRuntime`. Each step in the plan graph maps to a separate
`skill_run_id`. In Phase 2.1, these skill runs execute as independent async
tasks.

This creates a structural question: if each skill run produces its own
`AuditSeal`, what represents the plan itself at the audit level? The plan is
not itself a skill run — it is a coordinator that dispatches skill runs. Yet
the plan has its own `plan_id`, generates `plan_step_completed` WAL events
(ADR-0023 §3.2), and occupies a distinct causal position in the audit trail.

The `plan_step_completed` event is defined in `docs/concepts/events.md` as a
WAL-eligible event. Whether it should also be a seal boundary is the question
deferred to this sub-ADR.

This sub-ADR is tightly coupled to sub-ADR 0027a (hash chain topology):
- If Option C (per-workflow tree) is chosen in 0027a, then the plan must have
  its own seal as the tree root.
- If Option A or D (per-agent chain) is chosen in 0027a, plan seals are
  optional structural additions.

---

## Decision drivers

- **ADR-0023 plan_step_completed event**: plans already emit WAL events at
  step boundaries; these are natural seal candidates.
- **Audit completeness**: an audit of a multi-step plan should be traceable
  as a unit, not only as a bag of independent skill-run seals.
- **Plan crash semantics**: a plan that crashes mid-execution produces
  a partial set of skill-run seals; whether the plan itself has a seal
  affects how partial execution is detected.
- **Seal_unit orthogonality**: the `seal_unit: skill` default should remain
  the stable baseline; plan-level sealing is an extension, not a replacement.
- **Implementation cost**: adding a plan-level seal requires hooking into
  `PlanRuntime` lifecycle events (plan start, plan complete, plan abort).

---

## Options considered

### Option A: Plan is not a seal unit (skill runs only)

Only skill runs are sealed. The plan is represented in the audit trail only
through the `plan_id` field in each skill run's `AuditSeal` and `AuditContext`.

A verifier wanting to reconstruct a full plan execution would query all seals
with a matching `plan_id`.

**Pros:**
- No change to the existing `seal_unit: skill` baseline.
- Plan coordinator (`PlanRuntime`) does not need to be AuditSeal-aware.
- Simplest implementation path.

**Cons:**
- No single audit artifact represents "this plan ran from start to finish."
- Detecting a partial plan execution (steps 1–3 sealed, steps 4–5 missing
  due to crash) requires a verifier join across multiple seals plus knowledge
  of the expected step count.
- The `plan_step_completed` WAL event has no corresponding seal boundary.

### Option B: Plan has its own seal (plan_seal) + child skill runs reference it

A `PlanSeal` is produced at plan completion (or crash):

```json
// audit/seals/plan-<plan_id>.json
{
  "plan_id": "plan-xyz",
  "seal_kind": "plan",
  "step_count_expected": 5,
  "step_count_completed": 5,
  "child_seals": ["run-1/seal", "run-2/seal", ...],
  "chain_hash": "sha256:...",
  "prev_seal": "sha256:..."
}
```

Each child skill-run seal references the plan seal:

```json
{
  "run_id": "abc123",
  "plan_id": "plan-xyz",
  "plan_seal_ref": "sha256:..."
}
```

**Pros:**
- Single audit artifact for the full plan execution.
- Partial completion is immediately visible: `step_count_expected` vs
  `step_count_completed` mismatch.
- Enables Option C (per-workflow tree) in sub-ADR 0027a.

**Cons:**
- Requires extending `PlanRuntime` with AuditSeal lifecycle hooks.
- `PlanSeal` is a new artifact type, distinct from the per-skill `AuditSeal`.
- A crashed plan cannot produce a `PlanSeal` at completion; the plan seal
  must be written at crash time (partial) — this interacts with sub-ADR 0027d
  writer failure semantics.
- If `PlanRuntime` runs as an async task, the plan seal is produced after all
  child tasks complete; the ordering relative to child seals depends on the
  chain topology chosen in sub-ADR 0027a.

### Option C: Each skill run is independent; plan is metadata aggregation only

Same as Option A, but the verifier is responsible for aggregating skill-run
seals by `plan_id` and presenting a logical "plan view." No new seal artifact.
Plan metadata (expected step count, graph structure) is stored separately in
a non-seal manifest file.

**Pros:**
- `AuditSeal` schema stays simple (no `plan_seal_ref`, no `PlanSeal` type).
- Verifier complexity is pushed to query time, not write time.

**Cons:**
- The manifest file is not part of the hash chain; its integrity is not
  cryptographically attested.
- Verifier must know the plan structure to compute completeness — this is
  not self-contained in the seal artifacts.

### Option D: Plan dispatching agent carries plan in its own chain

The dispatching agent (the one running `PlanRuntime`) produces a seal for
the plan execution in its own per-agent chain (per Option A or D of
sub-ADR 0027a). Child skill runs running under different agents produce seals
in their own chains with `parent_seal_ref` back to the dispatching agent's
plan-level seal.

This is an extension of sub-ADR 0027a Option D (hybrid per-agent chain +
cross-agent reference links) where the plan coordinator is treated as a
"skill run" in the dispatcher agent's chain.

**Pros:**
- Reuses the per-agent chain topology without a new `PlanSeal` artifact type.
- Plan execution is represented by a seal in the dispatching agent's chain.
- Consistent with sub-ADR 0027a's recommended Option D.

**Cons:**
- The dispatching agent's plan "seal" represents a coordinator, not a skill
  run — the `run_id` / `skill` fields would need to express this distinction.
- Concurrent child skill runs completing after the plan seal is written would
  reference a "past" seal in the dispatcher's chain.

---

## Recommendation (proposed direction)

**Option B (plan has its own seal)** is the recommended direction for full
audit completeness, with **Option A as the minimum viable fallback**.

Rationale:
- Option B provides a self-contained audit artifact for plan execution, which
  is the correct answer for enterprise compliance where "prove this workflow
  ran to completion" is a required query.
- Option A is acceptable for the initial AuditSeal release (before plan-mode
  is widely used in compliance contexts) and reduces the implementation scope.
- Option C defers integrity to the verifier without cryptographic attestation
  — not suitable as the long-term design.
- Option D couples plan-mode representation to the topology decision in
  sub-ADR 0027a in a way that constrains both ADRs; decoupling is preferred.

**Sequencing recommendation**: implement Option A first (no plan seal,
`plan_id` metadata in child seals). Add Option B (PlanSeal) in the follow-up
PR that implements `reyn audit verify` for multi-step workflows. The Option B
implementation should be gated by resolution of sub-ADR 0027d (writer failure
semantics), since `PlanSeal` at crash time requires a defined partial-seal
behavior.

This recommendation should be re-evaluated at implementation start.

---

## Open questions

1. For Option B: what is the seal ordering between the `PlanSeal` and the
   last child skill-run seal in the per-agent chain? (The plan seal is written
   after all children complete; it may be interleaved with seals from other
   concurrent plans on the same agent.)
2. If a plan step is retried (after a child crash and recovery per ADR-0022/0023),
   does the retry produce a new child seal alongside the failed attempt's seal,
   or does the failed attempt's seal get amended?
3. Should `plan_step_completed` events (ADR-0023 §3.2) be represented as
   intermediate seals (`seal_unit: plan_step`) or remain as WAL-only events
   with no corresponding seal?
4. For the `step_count_expected` field in `PlanSeal`: the plan graph is
   dynamic (steps may be skipped or added by the LLM); how is the expected
   count established at plan start?

---

## Related

- ADR-0027: AuditSeal Separation (parent ADR)
- ADR-0027a: hash chain topology (directly coupled — topology choice affects
  whether Option D of this sub-ADR is feasible)
- ADR-0027b: config_hash scope
- ADR-0027d: writer failure semantics (PlanSeal at crash time depends on this)
- ADR-0023: Plan-Mode Forward Replay (plan execution model and WAL events)
- ADR-0022: Plan-Mode Crash Fail-Safe (crash recovery for plan-mode)
