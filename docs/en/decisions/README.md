# Architecture Decision Records (ADRs)

This directory captures the technical decisions and design trade-offs
behind Reyn's resume / persistence machinery (D-track and successor PRs).

Each ADR records:

- the **context** that prompted the decision,
- the **alternatives considered**,
- the **decision** that was made,
- the **consequences** (both desirable and undesirable),
- and **references** to commits, concept docs, and tracked follow-ups.

ADRs are immutable once accepted. New facts that contradict a decision
get a new ADR that supersedes the old one (the old one keeps its
historical value with status updated to "superseded by ADR-XXXX").

## Index

### Persistence model

| ADR | Topic |
|---|---|
| [0001](0001-state-model-wal-snapshot.md) | WAL + snapshot cache (transactional event-sourced replay) |
| [0002](0002-forward-replay-resume.md) | Forward-replay resume (no phase-head re-execution) |
| [0003](0003-op-purity-classification.md) | Op purity classification for step events |

### Memoization

| ADR | Topic |
|---|---|
| [0004](0004-memoization-key-design.md) | Memoization key: (op_invocation_id, phase, args_hash) |
| [0005](0005-volatile-field-stripping.md) | Volatile field stripping for memo stability |
| [0009](0009-visit-count-decrement-on-resume.md) | Pre-decrement visit_count on resume |

### Schema and lifecycle

| ADR | Topic |
|---|---|
| [0006](0006-schema-version-refuse-policy.md) | Schema version refuse + --reset (pre-1.0 policy) |
| [0010](0010-restore-cli-flags.md) | --no-restore / --reset CLI flag semantics |

### User intervention

| ADR | Topic |
|---|---|
| [0007](0007-bulk-resume-prompt-ux.md) | Bulk 2-choice resume prompt UX |
| [0008](0008-intervention-answer-buffering.md) | Intervention answer in-memory buffering (MVP) |

## Format

```markdown
# ADR-NNNN: <Short decision title>

**Status**: Accepted (YYYY-MM-DD)
**Track**: <D-track / PR-XYZ / ...>

## Context
What problem prompted this decision.

## Considered alternatives
- Option A: ... (pros / cons)
- Option B: ... (pros / cons)
- Option C: ...

## Decision
The chosen option + the primary reasoning.

## Consequences
Positive / negative / what's now possible / what's now precluded.

## References
- Commit / PR
- Related concept doc
- Tracked follow-up R-D items
```

## Discussion log

[discussion-log.md](discussion-log.md) — a chronological narrative of
the iterative refinements that produced these ADRs. Captures the
discarded paths so future readers understand what was tried and why.

## Related reading

- [Principles (P1–P8)](../concepts/principles.md) — invariants the
  decisions must respect
- [Skill resume](../concepts/skill-resume.md) — user-facing summary of
  the resulting machinery
- [Upgrade policy](../reference/upgrade-policy.md) — operator-facing
  consequence of ADR-0006
