# ADR-0027 Phase 1 Pre-Implementation User Judgment Gates

**Parent**: ADR-0027 AuditSeal Separation
**Related**: ADR-0027a / ADR-0027b / ADR-0027c / ADR-0027d
**Status**: Pending User Confirmation
**Created**: 2026-05-14

---

## Context

Before implementing ADR-0027 AuditSeal Phase 1 (single-agent hash chain
seal), five decisions must be confirmed by the user. Each sub-ADR
(0027a–d) carries a proposed recommendation, but the final choice is the
user's to make. This document consolidates all five gates into a single
checklist artifact so that confirmation can happen in one pass.

Each gate includes:
- the decision question
- the options considered (with pros/cons drawn from the corresponding sub-ADR)
- the recommendation and its rationale
- a checkbox for the user's final decision

Phase 1a implementation does **not** begin until all five gates are
resolved.

---

## Gate 1: Hash chain topology default (ADR-0027a)

**Decision question**: For the Phase 1 implementation, which hash chain
topology should AuditSeal use — per-agent chains only (Option A), or
per-agent chains with cross-agent reference links (Option D)?

### Options

**Option A — per-agent time-ordered single chain** *(recommended for Phase 1a)*

Each agent maintains its own time-ordered chain. A new skill run's seal
points `prev_seal` at the most recent seal produced by the same agent.

| | |
|---|---|
| Pros | Simple flat-list verifier; no cross-process coordination; single-agent use case fully covered |
| Cons | Cross-agent calls produce separate chains with no structural join; multi-agent compliance workflows cannot trace parent/child delegation structurally |

**Option D — hybrid: per-agent chain + optional `parent_seal_ref`**

Same as Option A, but seals include an optional `parent_seal_ref` pointing
at the calling agent's current chain head when a delegation occurs.

| | |
|---|---|
| Pros | Preserves Option A simplicity for single-agent runs; cross-agent relationships are machine-traversable; no global coordination required |
| Cons | Verifier must implement both flat-chain walk and cross-chain reference resolution (+1–2 days); `parent_seal_ref` population across process boundaries requires careful ordering |

**Option B** (global single chain) is ruled out — multi-process write
coordination is incompatible with Reyn's future multi-process expansion.

**Option C** (per-workflow tree) depends on a plan-level seal (Gate 3);
viable only after Gate 3 is resolved to Option B.

### Recommendation

Implement **Option A** in Phase 1a. Upgrade to **Option D** in Phase 1c
(after the plan-mode and verifier work is complete). The `parent_seal_ref`
field can be added to the schema as an optional extension without breaking
any Phase 1a seals.

### User decision

- [ ] Option A (Phase 1a baseline, upgrade to D in Phase 1c) — *recommended*
- [ ] Option D (implement cross-agent refs from the start)
- [ ] Other (describe):

---

## Gate 2: config_hash scope (ADR-0027b)

**Decision question**: What should be hashed to produce the `config_hash`
field in `AuditContext` — skill definition only (Option B), model settings
only (Option C), or a tiered structure of multiple independent sub-hashes
(Option D)?

### Options

**Option A — full reyn.yaml hash**

| | |
|---|---|
| Pros | Single file, simple to implement; captures all config changes |
| Cons | Any unrelated section change (e.g., `logging.level`) invalidates the seal — excessive noise in practice |

**Option B — skill definition hash only**

Hash the skill's `skill.md` and all referenced phase files.

| | |
|---|---|
| Pros | Directly answers "was this skill's definition changed?"; stable against unrelated reyn.yaml changes; skill-granular |
| Cons | Does not detect model provider switches or OS-level config changes that affect execution |

**Option C — model settings hash only**

Hash effective provider, model name, and inference parameters.

| | |
|---|---|
| Pros | Directly answers "was the same LLM used?"; aligns with Hermes #487 reproducibility positioning |
| Cons | Does not detect skill definition changes; model version aliases are unstable |

**Option D — tiered: multiple independent sub-hash fields** *(recommended)*

```json
{
  "config_hash": {
    "skill_def": "sha256:...",
    "model_cfg": "sha256:...",
    "os_cfg":    "sha256:..."
  }
}
```

Verifiers check any or all sub-hashes independently, depending on the
compliance requirement. Unknown sub-hash fields are ignored by older
verifiers (forward compatible).

| | |
|---|---|
| Pros | Maximum flexibility; no noise from unrelated changes; covers both compliance-auditability and reproducibility simultaneously |
| Cons | Three hash inputs to define and maintain; `os_cfg` scope requires a sub-decision; verifier error reporting must identify which sub-hash mismatched |

Minimal initial scope for Option D:

| Sub-hash | Content hashed |
|---|---|
| `skill_def` | `skill.md` + all referenced phase files (canonical content) |
| `model_cfg` | Effective provider + model name + key inference params (temperature, max_tokens) |
| `os_cfg` | `audit.*` section of reyn.yaml (initially narrow; expand on demand) |

If the tiered approach proves too complex for the initial release, fall back
to **Option B** with a design note that `model_cfg` will be added in a
follow-up.

### Recommendation

**Option D (tiered)** for full coverage. **Option B as minimum viable
fallback** if Option D implementation cost is too high for Phase 1a.

### User decision

- [ ] Option D (tiered, full coverage) — *recommended*
- [ ] Option B (skill definition hash only, as minimum viable fallback)
- [ ] Other (describe):

---

## Gate 3: Plan-mode seal boundary (ADR-0027c)

**Decision question**: Should a plan execution (spawning multiple
concurrent skill runs) produce its own `PlanSeal` artifact, or should
plan-level audit coverage be achieved by querying child skill-run seals
by `plan_id`?

### Options

**Option A — plan is not a seal unit; skill runs only** *(recommended for Phase 1a)*

Only skill runs are sealed. Plan-level audit is reconstructed by querying
all seals sharing a `plan_id`.

| | |
|---|---|
| Pros | No change to `seal_unit: skill` baseline; `PlanRuntime` does not need AuditSeal lifecycle hooks; simplest implementation path |
| Cons | No single artifact proves "this plan ran to completion"; detecting partial execution requires a join across multiple seals; `plan_step_completed` WAL events have no corresponding seal boundary |

**Option B — plan has its own `PlanSeal` + child seals reference it**

A `PlanSeal` is produced at plan completion (or crash) with
`step_count_expected` / `step_count_completed` fields; child skill-run
seals carry a `plan_seal_ref`.

| | |
|---|---|
| Pros | Single audit artifact for the full plan execution; partial completion immediately visible via step count mismatch; enables Option C topology in Gate 1 |
| Cons | Requires extending `PlanRuntime` with AuditSeal hooks; `PlanSeal` is a new artifact type; crash-time partial seal behavior must be resolved (depends on Gate 4) |

**Option C — skill runs independent, verifier aggregates by `plan_id`**

Same as Option A, but the manifest file is stored separately (not in the
hash chain). Integrity verification is pushed to the verifier.

| | |
|---|---|
| Pros | `AuditSeal` schema stays simple |
| Cons | Manifest integrity is not cryptographically attested; verifier must know plan structure to check completeness |

**Option D — dispatching agent carries plan in its own chain**

The plan coordinator is treated as a "skill run" in the dispatcher agent's
chain; child runs carry `parent_seal_ref` back to it (couples to Gate 1
Option D).

| | |
|---|---|
| Pros | Reuses per-agent topology without a new artifact type |
| Cons | Distinguishing a plan "seal" from a skill-run seal requires schema extensions; couples this decision to Gate 1 topology |

### Recommendation

**Option A** for Phase 1a (no plan seal; `plan_id` metadata in child seals).
**Option B** added in the follow-up PR that implements `reyn audit verify`
for multi-step workflows. Option B is gated on Gate 4 resolution (writer
failure semantics for `PlanSeal` at crash time).

### User decision

- [ ] Option A (Phase 1a baseline, add Option B in follow-up) — *recommended*
- [ ] Option B (PlanSeal from the start)
- [ ] Other (describe):

---

## Gate 4: Writer failure defaults (ADR-0027d)

**Decision question**: What should the OS do when the AuditContext write
(at skill start) or the AuditSeal write (at skill completion) fails — fail
open (continue without the record), fail closed (block/abort), or a
configurable mode that operators choose per environment?

### Options

**Option A — fail-open (skill runs; missing record detected post-hoc)**

Any write failure is logged; the skill proceeds normally.

| | |
|---|---|
| Pros | Skill function is never affected by audit infrastructure failures; long-running skills are not aborted |
| Cons | A compliance gap may go undetected until an audit sweep; if context write fails silently, the eventual seal has no `run_id` anchor |

**Option B — fail-closed (context write blocks skill start; seal write retries then emits event)**

Context write failure prevents skill start. Seal write retries N times;
on exhaustion, emits `seal_write_failed` and treats the run as having an
incomplete audit trail.

| | |
|---|---|
| Pros | Strong compliance guarantee; low-cost block at context-write time (nothing computed yet); explicit retry is itself auditable |
| Cons | Transient disk failures abort user requests before any work is done; seal write failure after a long run cannot practically abort (all output already exists) |

**Option C — degraded mode (in-memory hold + background retry)**

On failure, hold in memory and retry in a background task within a
configurable window. If the window expires, emit `seal_degraded` and
fall to fail-open.

| | |
|---|---|
| Pros | Handles transient I/O errors without failing the skill; `seal_degraded` creates a detectable verifier signal |
| Cons | In-memory hold is lost on process crash — the same scenario AuditSeal is designed to detect; background retry adds OS complexity |

**Option D — configurable per reyn.yaml** *(recommended)*

```yaml
audit:
  writer_failure:
    context: fail-open   # or: fail-closed
    seal:    fail-open   # or: fail-closed, degraded
    retry_count: 3
```

Operators choose the mode for their environment. Default is fail-open for
both. Enterprise operators opt in to fail-closed.

| | |
|---|---|
| Pros | Accommodates both OSS and enterprise requirements; no surprising default behavior |
| Cons | Increases configuration surface; all three modes must be implemented regardless of active mode |

### Recommendation

**Option D (configurable)** with fail-open defaults. Minimum viable
implementation for Phase 1a: implement **fail-open only** (emit
`seal_write_failed` event on failure). The configurable fail-closed path
is added in a follow-up PR targeted at enterprise compliance certification.

### User decision

- [ ] Option D (configurable; fail-open default; fail-closed as Phase 2 enterprise add-on) — *recommended*
- [ ] Option A (fail-open only, permanent)
- [ ] Other (describe):

---

## Gate 5: AuditContext schema scope

**Decision question**: How broad should the initial `AuditContext` schema
be — a narrow compliance-essential set, or an expanded set that also
captures runtime observability fields?

### Options

**Option A — narrow (compliance-essential only)** *(recommended for Phase 1a)*

```json
{
  "run_id":          "abc123",
  "skill":           "researcher",
  "invoked_by":      "user@example.com",
  "original_request": "...",
  "model":           "gemini-2.5-flash-lite",
  "model_version":   "...",
  "config_hash":     { ... },
  "started_at":      "2026-05-14T..."
}
```

Fields are the minimum required for a verifier to answer: who ran what,
with which model, under which configuration, and when.

| | |
|---|---|
| Pros | Small schema; easy to extend; avoids committing to fields whose semantics are unclear before implementation |
| Cons | Observability use cases (e.g., latency attribution, token budget at start) require separate lookup in Events |

**Option B — expanded (compliance + runtime observability)**

Adds fields such as `token_budget_at_start`, `plan_id` (if invoked as a
plan step), `workspace_path`, `agent_id`, and `invocation_path` (the
skill call stack for nested `run_skill` invocations).

| | |
|---|---|
| Pros | Single record answers both compliance and debugging queries; `plan_id` in the context record enables plan-level grouping without a `PlanSeal` (supports Gate 3 Option A) |
| Cons | Wider schema increases writer complexity; some fields (e.g., `token_budget_at_start`) may not be available at context-write time depending on OS lifecycle ordering |

### Recommendation

**Option A (narrow)** for Phase 1a. Add `plan_id` (and optionally `agent_id`)
as the first expansion in Phase 1b, since `plan_id` is required for the
Gate 3 Option A verifier query. Defer `token_budget_at_start` and
`invocation_path` to Phase 2 or later.

### User decision

- [ ] Option A (narrow; add `plan_id` in Phase 1b) — *recommended*
- [ ] Option B (expanded from the start)
- [ ] Other (describe):

---

## Summary

| Gate | Topic | Recommendation | Decision |
|---|---|---|---|
| 1 | Hash chain topology | Option A → D upgrade in Phase 1c | [ ] |
| 2 | config_hash scope | Option D (tiered); Option B as fallback | [ ] |
| 3 | Plan-mode seal boundary | Option A → B in Phase 1b/1c | [ ] |
| 4 | Writer failure defaults | Option D (configurable); fail-open only in Phase 1a | [ ] |
| 5 | AuditContext schema scope | Option A (narrow); add `plan_id` in Phase 1b | [ ] |

Phase 1a implementation begins after all five gates are resolved.

---

## Phase 1 implementation sequence (reference)

Phase 1 was proposed as a six-week sequence:

| Phase | Scope | Gates consumed |
|---|---|---|
| **Phase 1a** (weeks 1–2) | AuditContext writer + AuditSeal generator + hash chain (Option A topology) + `reyn.yaml` opt-in + fail-open writer | Gates 1, 4, 5 |
| **Phase 1b** (weeks 3–4) | `reyn audit verify` CLI for single-agent chains + `plan_id` in AuditContext + config_hash implementation (Gate 2 choice) | Gates 2, 5 expansion |
| **Phase 1c** (weeks 5–6) | Cross-agent `parent_seal_ref` (if Gate 1 → D) + PlanSeal (if Gate 3 → B) + configurable writer failure (if Gate 4 enterprise path) | Gates 1, 3, 4 extensions |

---

## Related

- [ADR-0027: AuditSeal Separation](0027-audit-seal-separation.md)
- [ADR-0027a: Hash chain topology](0027a-audit-seal-hash-chain-topology.md)
- [ADR-0027b: config_hash scope](0027b-audit-seal-config-hash-scope.md)
- [ADR-0027c: Plan-mode integration](0027c-audit-seal-plan-mode-integration.md)
- [ADR-0027d: Writer failure semantics](0027d-audit-seal-writer-failure-semantics.md)
