# ADR-0027a: Hash chain topology for AuditSeal

**Status**: Proposed
**Date**: 2026-05-13
**Depends on**: ADR-0027 (AuditSeal Separation)

---

## Context

ADR-0027 defines `AuditSeal` as a separate compliance layer that seals each
skill run with a hash chain (`chain_hash` + `prev_seal` fields). The parent
ADR deferred the question of **what constitutes a chain**: which `prev_seal`
does a new seal point at, and how do chains from concurrent or nested agents
compose?

This question was deferred because the answer depends on the multi-agent
concurrency model (multi-process plan described in ADR-0023 amendment §2.1)
and the audit verifier implementation strategy — both of which were not
finalized at ADR-0027 write time.

The chain topology choice has direct consequences for:

- How a `reyn audit verify` command walks the seal chain
- Whether two concurrently executing agents produce a single verifiable chain
  or independent verifiable chains
- How plan-mode (one plan spawning multiple skill runs) is represented
  (detailed in sub-ADR 0027c)

---

## Decision drivers

- **Multi-agent concurrent run**: Reyn supports `delegate_to_agent` and
  `plan`-mode spawning multiple concurrent skill runs, possibly across
  multiple agents.
- **Multi-process plan**: ADR-0023 Phase 2.1 establishes that a plan tool
  creates an async task per step; future multi-process expansion would
  push this further.
- **Audit verifier implementation cost**: the verifier must walk the chain
  in a deterministic, reproducible way.
- **Plan-mode integration**: seal_unit interacts with plan boundaries (see
  sub-ADR 0027c for the dedicated analysis).
- **Enterprise compliance expectation**: regulated environments expect an
  unbroken chain per auditable unit (workflow / agent / run).
- **OSS light-user usability**: single-process users should not need to
  manage forest topology.

---

## Options considered

### Option A: Per-agent time-ordered single chain

Each agent maintains its own time-ordered chain. A new skill run's seal
has `prev_seal` pointing at the most recent seal produced by the **same
agent**.

```
agent-alice:  [seal-1] → [seal-2] → [seal-3]
agent-bob:    [seal-1] → [seal-2]
```

**Pros:**
- Simple: each agent's seal directory is an independent linked list.
- No cross-process ordering coordination.
- Verifier walks a single flat chain per agent.

**Cons:**
- Cross-agent calls (e.g., `delegate_to_agent`) produce separate chains
  with no structural join; the parent/child relationship is only in
  `run_id` metadata.
- Multi-agent workflows require a verifier that can follow cross-chain
  references if full-workflow integrity is needed.
- No natural representation of "this chain is the child of that chain."

### Option B: Global single chain (all agents share one chain)

All agents write to a single ordered chain. `prev_seal` always points at
the globally most recent seal, regardless of which agent produced it.

```
global: [alice/seal-1] → [bob/seal-1] → [alice/seal-2] → [bob/seal-2]
```

**Pros:**
- Single chain; verifier has one traversal path.
- Complete ordering of all activity across all agents.

**Cons:**
- Multi-process writes require a distributed lock or serialization point,
  creating a bottleneck and a coordination failure mode.
- Race conditions under concurrent skill completions produce non-deterministic
  chain ordering — different runs of the same workflow produce different chains,
  complicating bit-exact verification.
- Fundamentally at odds with Reyn's eventual multi-process expansion plan.

### Option C: Per-workflow tree (forest of seal trees)

Each top-level user request (workflow) roots its own seal tree. Child skill
runs (spawned by plan or delegate) form sub-trees, each seal referencing its
parent seal.

```
workflow-w1:
  root (plan seal)
  ├── [step-1/seal]
  ├── [step-2/seal]
  └── [step-3/seal]

workflow-w2:
  root (plan seal)
  └── [step-1/seal]
```

**Pros:**
- Natural structural representation of plan-mode: the plan seal is the
  root, child skill-run seals reference it.
- Full-workflow integrity check is a tree traversal, not a cross-chain
  reference hunt.
- No global ordering coordination needed.

**Cons:**
- Requires a plan-level seal (addressed in sub-ADR 0027c); not straightforward
  if plans are treated as metadata aggregation only (Option C of sub-ADR 0027c).
- Forest management: each workflow root must be tracked; orphaned roots
  (crashed plans) need gap-handling policy.
- Higher verifier implementation complexity than a flat per-agent chain.

### Option D: Hybrid — per-agent chain + cross-agent reference links

Each agent maintains its own chain (same as Option A), but seals include
an optional `parent_seal_ref` field pointing at the seal of the calling
agent's current head when a delegation occurs.

```
agent-alice:  [seal-1] → [seal-2 (plan spawned)]
                                ↓ parent_ref
agent-bob:    [seal-1 parent=alice/seal-2] → [seal-2]
```

**Pros:**
- Preserves Option A simplicity for single-agent cases.
- Cross-agent relationships are explicit and machine-traversable.
- No global coordination; per-agent chains are independent.

**Cons:**
- Verifier must implement both flat chain walk and cross-chain reference
  resolution — more complex than either Option A or C alone.
- `parent_seal_ref` introduces a coupling between agents that must be
  populated at delegation time; if the delegation crosses a process boundary,
  the reference may arrive after the child seal is written.

---

## Recommendation (proposed direction)

**Option D (hybrid)** is the recommended direction, with **Option A as the
fallback** if cross-agent reference management proves too complex during
implementation.

Rationale:
- Option A is the correct baseline for single-agent and single-process
  deployments (the current common case).
- Option D extends Option A without breaking it: the `parent_seal_ref`
  field is optional; a verifier that ignores it degrades gracefully to
  Option A behavior.
- Option B is ruled out due to multi-process coordination requirements.
- Option C depends on a plan-level seal, which is an open question resolved
  in sub-ADR 0027c. Option D does not require that resolution.

**Decision at implementation time**: if sub-ADR 0027c resolves to "plan has
its own seal" (Option B or D of that sub-ADR), revisit Option C — it becomes
viable and may be preferable for workflow-level audit completeness.

This recommendation should be re-evaluated at implementation start.

---

## Open questions

1. When a delegation crosses a process boundary, is `parent_seal_ref`
   populated synchronously (blocking delegation start) or asynchronously
   (written as an amendment to the child seal after the child is sealed)?
2. For Option D, what is the seal ordering within a single agent's chain
   when concurrent skill runs complete out of order? (Time-of-completion
   ordering vs. time-of-start ordering.)
3. If a delegated agent crashes before producing a seal, does the parent
   agent's chain have a gap entry or is the gap only detectable via the
   AuditContext record?

---

## Related

- ADR-0027: AuditSeal Separation (parent ADR)
- ADR-0027b: config_hash scope
- ADR-0027c: seal_unit and plan-mode integration (topology choice interacts
  directly with whether plans have their own seals)
- ADR-0027d: writer failure semantics
- ADR-0023: Plan-Mode Forward Replay (multi-agent async dispatch context)
