# ADR-0027d: AuditContext writer failure semantics

**Status**: Proposed
**Date**: 2026-05-13
**Depends on**: ADR-0027 (AuditSeal Separation)

---

## Context

ADR-0027 defines two OS-managed write operations in the AuditSeal lifecycle:

1. **AuditContext write** — at skill run start: OS writes
   `audit/context/<run_id>.json` before the first phase executes.
2. **AuditSeal write** — at skill run completion: OS writes
   `audit/seals/<run_id>.json` after the last phase completes.

The parent ADR deferred the question of **what happens when either write
fails**. This matters because:

- AuditContext is written before the skill runs; a failure here blocks skill
  start (or must be explicitly allowed to proceed without the context record).
- AuditSeal is written after the skill completes; a failure here means the
  skill produced output but has no seal — a compliance gap rather than a
  functional failure.

These two write points have different failure modes and different consequences,
but share the core design tension: **fail-open** (continue without the record)
vs. **fail-closed** (block or abort when the audit record cannot be written).

The AuditSeal feature is opt-in (default off per ADR-0027 §2). The failure
semantics must be coherent whether the feature is enabled or disabled, and
must accommodate both enterprise compliance expectations and OSS light-user
tolerance.

---

## Decision drivers

- **Enterprise compliance expectation**: regulated environments typically
  require fail-closed behavior — if the audit record cannot be written,
  the audited action should not proceed (or should be flagged as an
  integrity failure).
- **OSS light-user tolerance**: light users who enable AuditSeal as a
  "nice to have" may prefer fail-open — a transient disk write failure
  should not abort a long skill run.
- **Ephemeral failure tolerance**: disk write failures are often transient
  (filesystem not yet mounted, temporary I/O error). A retry loop may
  resolve the failure without surfacing it.
- **Init/teardown timing**: the AuditContext write occurs at skill start,
  before any skill work is done — the cost of a fail-closed block is low
  (nothing was computed yet). The AuditSeal write occurs at skill completion,
  after all skill work is done — the cost of a fail-closed abort is high
  (all work is lost from a user perspective).
- **Gap detection**: if a seal is missing, an audit verifier must be able to
  distinguish "skill ran, seal not written" from "skill never ran." This
  requires either the AuditContext record (written at start) to be present,
  or a gap marker.
- **Plan-mode interaction**: sub-ADR 0027c Option B (PlanSeal) introduces a
  third write point at plan completion; its failure semantics should be
  consistent with this sub-ADR's decision.

---

## Options considered

### Option A: Fail-open (skill runs, missing audit record detected post-hoc)

If either write fails, the OS logs the failure and continues. The skill run
proceeds normally. The missing record is a **known gap** that appears as an
anomaly in an audit sweep.

**Context write failure**: skill starts without an AuditContext record.
**Seal write failure**: skill completes, output delivered, but no seal.

**Pros:**
- Skill function is unaffected by audit infrastructure failures.
- Long-running skills are not aborted due to transient I/O errors.
- Users who enabled AuditSeal for lightweight auditability get skill
  execution with degraded audit coverage — acceptable for OSS light users.

**Cons:**
- A compliance gap may go undetected until an audit sweep. In regulated
  environments, "the skill ran but we have no record" may be an
  unacceptable audit outcome.
- If the context write fails silently, there is no `run_id` anchor for
  the eventual seal — the verifier cannot link the two.

### Option B: Fail-closed (skill blocked or aborted on audit write failure)

If the **context write** fails, the skill does not start. The OS returns an
error to the caller.

If the **seal write** fails, the OS retries up to N times (configurable),
then emits a `seal_write_failed` event and treats the skill run as having
an incomplete audit trail. Whether the skill output is delivered to the
caller depends on the retry outcome.

**Pros:**
- Strong compliance guarantee: audit records exist for every skill run that
  is allowed to proceed.
- Context write failure at skill start is low-cost to block (nothing was
  computed yet).
- Explicit retry policy is auditable itself.

**Cons:**
- A transient disk write failure at skill start aborts the user's request
  before any work is done — potentially confusing.
- Seal write failure after a long skill run cannot easily abort (the skill
  output already exists); forcing an abort here is destructive from the
  user perspective.
- Configuring retry counts adds operational complexity.

### Option C: Degraded mode (in-memory hold, background retry)

On write failure, the OS holds the record in memory and retries the write
in a background task. If the background retry succeeds within a configurable
window, the record lands normally. If the window expires without success,
the OS emits a `seal_degraded` event and proceeds with fail-open semantics.

**Context write**: skill start is delayed until the context write succeeds
or the retry window expires (then fail-open).
**Seal write**: skill output is delivered; background retry writes the seal
asynchronously.

**Pros:**
- Handles transient I/O errors gracefully without failing the skill.
- Provides a window for recovery before declaring a gap.
- The `seal_degraded` event creates a detectable signal for the verifier.

**Cons:**
- In-memory hold means the record is lost if the process crashes during
  the retry window — the same crash scenario that AuditSeal is designed
  to make auditable.
- Background retry complexity increases the OS implementation surface.
- The retry window timing must be coordinated with skill lifecycle events.

### Option D: Configurable per reyn.yaml (operator choice)

The failure semantics are controlled by a `reyn.yaml` configuration option:

```yaml
audit:
  writer_failure: fail-open   # or: fail-closed, degraded
  writer_retry_count: 3       # applies to fail-closed and degraded modes
  writer_retry_delay_ms: 500
```

Operators choose the mode appropriate for their environment. Enterprise
operators select `fail-closed`; OSS light users leave the default
(`fail-open`).

**Pros:**
- Accommodates both enterprise and OSS requirements without forcing a single
  policy.
- No surprising behavior: operators declare their expectation explicitly.

**Cons:**
- Increases configuration surface area.
- Operators who misconfigure (e.g., `fail-closed` in a test environment
  with no audit directory) will get confusing skill start failures.
- The implementation must support all three modes, increasing complexity
  regardless of which mode is active.

---

## Recommendation (proposed direction)

**Option D (configurable)** is the recommended direction, with the following
defaults:

| Mode | Default for | Rationale |
|---|---|---|
| `fail-open` | default (AuditSeal disabled) | no-op; AuditSeal is off |
| `fail-open` | AuditSeal enabled, no explicit config | OSS light users; skill functionality > audit completeness |
| `fail-closed` | explicit enterprise config | regulated environment opt-in |

The **context write** and **seal write** failure modes are configurable
independently:

```yaml
audit:
  writer_failure:
    context: fail-open    # skill starts even if context write fails
    seal: fail-open       # skill output delivered even if seal write fails
    retry_count: 3        # applies to both; 0 = no retry
```

Rationale for asymmetric defaults:
- Context write failure at skill start is less costly to block; enterprise
  users may prefer `fail-closed` here.
- Seal write failure after a long skill run is costly to block; even in
  enterprise deployments, `degraded` or `fail-open` with an explicit event
  is more practical than aborting the skill output.

**Minimum viable implementation (initial AuditSeal release)**: implement
`fail-open` mode only, with a `seal_write_failed` event emitted on failure.
The configurable `fail-closed` path is added in a follow-up PR targeted at
enterprise compliance certification. This sequencing avoids blocking the
initial AuditSeal release on the more complex retry/mode-selection logic.

This recommendation should be re-evaluated at implementation start.

---

## Open questions

1. If the context write fails and the skill proceeds (fail-open), the
   eventual seal write has no `context_hash` anchor. Should the seal be
   written with a `context_hash: null` marker, or should the seal be omitted
   entirely (creating a gap)?
2. For the `fail-closed` context write mode: what is the observable behavior
   from the caller's perspective? (Error event emitted? Specific error message
   in the skill start response?)
3. For the retry mechanism (Option D `retry_count`): should the retry be a
   synchronous block at write time, or an async background task? The
   in-memory-hold risk from Option C applies to any async retry.
4. For plan-mode (sub-ADR 0027c Option B PlanSeal): should the `PlanSeal`
   writer failure semantics inherit from this sub-ADR's `seal` mode, or be
   independently configurable?

---

## Related

- ADR-0027: AuditSeal Separation (parent ADR)
- ADR-0027a: hash chain topology
- ADR-0027b: config_hash scope
- ADR-0027c: seal_unit and plan-mode integration (PlanSeal writer failure
  depends on this sub-ADR's resolution)
- ADR-0001: WAL + snapshot (crash recovery context for in-memory hold risk)
- ADR-0013: Exception-aware crash lifecycle (skill abort semantics)
