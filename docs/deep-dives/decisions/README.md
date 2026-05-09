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
| [0014](0014-wal-size-safety-net.md) | WAL size safety net trigger |

### Memoization

| ADR | Topic |
|---|---|
| [0004](0004-memoization-key-design.md) | Memoization key: (op_invocation_id, phase, args_hash) |
| [0005](0005-volatile-field-stripping.md) | Volatile field stripping for memo stability |
| [0009](0009-visit-count-decrement-on-resume.md) | Pre-decrement visit_count on resume |
| [0011](0011-world-purity-memo-invalidation.md) | World-purity memo invalidation on resume |
| [0015](0015-llm-result-workspace-ref.md) | LLM result workspace ref threshold |

### Schema and lifecycle

| ADR | Topic |
|---|---|
| [0006](0006-schema-version-refuse-policy.md) | Schema version refuse + --reset (pre-1.0 policy) |
| [0010](0010-restore-cli-flags.md) | --no-restore / --reset CLI flag semantics |
| [0013](0013-exception-aware-crash-lifecycle.md) | Exception-aware skill completion in finally clause |

### Resume UX and policy

| ADR | Topic |
|---|---|
| [0007](0007-bulk-resume-prompt-ux.md) | ~~Bulk 2-choice resume prompt UX~~ — superseded by [0012](0012-auto-resume-default.md) |
| [0012](0012-auto-resume-default.md) | Auto-resume default + retry policy |

### User intervention

| ADR | Topic |
|---|---|
| [0008](0008-intervention-answer-buffering.md) | ~~In-memory answer buffer (MVP)~~ — superseded by [0016](0016-durable-answer-buffer.md) |
| [0016](0016-durable-answer-buffer.md) | Durable intervention answer buffer |

### Multi-agent and nested skills

| ADR | Topic |
|---|---|
| [0017](0017-parent-run-id-nested-skill-path.md) | parent_run_id for nested skill path display |
| [0018](0018-cross-agent-discard-notify.md) | Cross-agent discard chain notification |

### Web UI scope

| ADR | Topic |
|---|---|
| [0019](0019-openui-reyn-internal-framing.md) | OpenUI reframed as Reyn-internal contract |

### Permissions

| ADR | Topic |
|---|---|
| [0020](0020-skill-only-permissions.md) | Skill-only permissions — Phase.permissions field removed (案 2) |

### Architecture

| ADR | Topic |
|---|---|
| [0026](0026-unified-tool-registry.md) | Unified tool registry — single ToolDefinition for router and phase surfaces (Proposed) |
| [0027](0027-audit-seal-separation.md) | AuditSeal を Events (P6) から分離 — compliance と operational の責務境界 (Proposed) |

> Web UI direction (= 元 ADR-0028) は positioning doc に re-class、 `docs/deep-dives/research/positioning/web-ui-direction.md` 参照。 現在 vision は **`reyn chat` (= local + embedded Web UI server を session bind で同梱) + `reyn serve` / `reyn client` (= 別軸で multi-user / multi-device path)**。 embedded thesis は `reyn chat` 内に温存、 server-client 軸を併設。 実現性検討は未着手。

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
