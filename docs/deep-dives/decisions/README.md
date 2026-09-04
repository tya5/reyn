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

One consequence, because it has been missed: **an ADR that names a string
makes renaming that string an ADR decision, not a sweep.** If `collect="attached"`
appears in an accepted ADR's body, changing the value to something else falsifies
a sentence in a document nobody may edit — so the cost is a superseding ADR, not a
rename PR across code, catalog, prompt and fixtures. Before proposing to rename an
LLM-visible string, grep `decisions/` for it.

The distinction that matters is whether the ADR **decides** the change or
**states** the thing being changed. ADR-0040 names `delegate_to_agent` as a tool it
decides to retire; retiring it implements the ADR. The same ADR also states that
`collect="attached"` creates nothing at all; renaming that value would make the
sentence false. Same file, opposite relationship, and only the second one needs a
new ADR.

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
| [0031](0031-config-cascade-3-layer.md) | 3-layer config cascade — deprecate `<project>/.reyn/config.yaml` (**Accepted** 2026-05-09) |

### Resume UX and policy

| ADR | Topic |
|---|---|
| [0007](0007-bulk-resume-prompt-ux.md) | ~~Bulk 2-choice resume prompt UX~~ — superseded by [0012](0012-auto-resume-default.md) |
| [0012](0012-auto-resume-default.md) | Auto-resume default + retry policy |
| [0038](0038-user-facing-time-travel-rewind.md) | User-facing time-travel — global consistent-cut rewind + PITR snapshot generations (**Accepted + Implemented** 2026-06-13) |

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
| [0034](0034-a2a-task-lifecycle.md) | A2A task lifecycle (FP-0001) (**Accepted + Implemented** 2026-05-16) — Components 1–3 (intervention override, RunEntry persistence) superseded by [0041](0041-intervention-ownership-and-channel-pinning.md); Components 4–5 (router endpoints, Agent Card capabilities) stand unchanged |
| [0040](0040-task-as-os-concept.md) | `task` as an OS-level concept — vocabulary, collection, and who authors state (**Accepted** 2026-08-10, not yet implemented; extends 0034) — sequencing in [proposal 0067](../proposals/0067-task-model-and-arbiter.md) |
| [0041](0041-intervention-ownership-and-channel-pinning.md) | Intervention ownership and channel pinning — supersedes 0034 Components 1–3 only (**Accepted + Implemented**, written after the fact 2026-08-10 to close a 3-month doc/code gap; see [#4016](https://github.com/tya5/reyn/issues/4016)) |

### Web UI scope

| ADR | Topic |
|---|---|
| [0019](0019-openui-reyn-internal-framing.md) | OpenUI reframed as Reyn-internal contract |

### Permissions

| ADR | Topic |
|---|---|
| [0020](0020-skill-only-permissions.md) | Skill-only permissions — Phase.permissions field removed (案 2) |
| [0037](0037-sandbox-permission-separation.md) | Sandbox / permission separation — agent-level containment unification (**Accepted + Implemented** 2026-06-05) |

### Architecture

| ADR | Topic |
|---|---|
| [0026](0026-unified-tool-registry.md) | Unified tool registry — single ToolDefinition for router and phase surfaces (Proposed) |
| [0027](0027-audit-seal-separation.md) | AuditSeal を Events (P6) から分離 — compliance と operational の責務境界 (Proposed) |
| [0027a](0027a-audit-seal-hash-chain-topology.md) | Hash chain topology for AuditSeal (Proposed, depends on 0027) |
| [0027b](0027b-audit-seal-config-hash-scope.md) | `config_hash` scope for AuditSeal (Proposed, depends on 0027) |
| [0027c](0027c-audit-seal-plan-mode-integration.md) | `seal_unit` and plan-mode integration for AuditSeal (Proposed, depends on 0027 + 0023) |
| [0027d](0027d-audit-seal-writer-failure-semantics.md) | AuditContext writer failure semantics (Proposed, depends on 0027) |
| [0029](0029-mcp-install-permission.md) | `mcp_install` permission — install-time gating として permission system に追加 (Proposed) |
| [0030](0030-universal-secret-handling.md) | Universal secret handling — `${VAR}` 全 yaml + `~/.reyn/secrets.env` + `reyn secret` CLI (Proposed) |
| [0033](0033-rag-extensible-os.md) | RAG-extensible OS — `embed` / `index_*` / `recall` ops + `index_docs` stdlib + `IndexBackend` protocol (**Accepted** 2026-05-10) |

> Web UI direction (= 元 ADR-0028) は positioning doc に re-class、 `docs/deep-dives/research/positioning/web-ui-direction.md` 参照。 現在 vision は **`reyn chat` (= local + embedded Web UI server を session bind で同梱) + `reyn serve` (= explicit long-running server、 browser からアクセス)** の 2 commands。 業界慣行 (Ollama / vLLM / LangGraph) の `<tool> serve` pattern に整合、 remote TUI client は workspace location semantics の懸念で当面 scope 外 (= browser remote access で代替、 将来 concrete demand 出たら再判断)。 embedded thesis は `reyn chat` 内に温存。 実現性検討は未着手。

### Runtime interaction model

| ADR | Topic |
|---|---|
| [0035](0035-phase-tool-calls-unification.md) | Phase op-execution via native tool_calls (Phase ↔ chat/planner unification) (**Accepted, fully implemented** 2026-06-02) |
| [0036](0036-history-compaction-force-close-unification.md) | Chat/plan/phase within-unit history + compaction + force-close unification (Fork 1: RouterLoop convergence) (**Accepted**) — the PR-F2b force-close handoff cap section superseded by [0042](0042-force-close-layer2-removal.md); the rest stands unchanged |
| [0042](0042-force-close-layer2-removal.md) | force-close layer② removal — spill replaces the consolidate-and-retry path; **layer① (turn-budget force-close) is unaffected and still live** (**Accepted + Implemented** 2026-08-12; supersedes 0036's PR-F2b section only; see [#4381](https://github.com/tya5/reyn/issues/4381)) |
| [0044](0044-overflow-recovery-ladder.md) | Overflow recovery — one cause-independent ladder (byte and token take the same rungs), spill first, predicate terminals in place of `max_iterations`, and `Spillability` declared by the producer (**Accepted** 2026-08-30, verified against the merged #5547; extends [0042](0042-force-close-layer2-removal.md), supersedes nothing; see [#5531](https://github.com/tya5/reyn/issues/5531)) |
| [0045](0045-spill-granularity.md) | Spill granularity — one request per (compartment × `Spillability`) instead of per turn, after a live incident where rung ①'s request count tracked the candidate count (**Accepted** 2026-09-02, verified against the merged #5596; supersedes [0044](0044-overflow-recovery-ladder.md)'s rung-① granularity only; see [#5592](https://github.com/tya5/reyn/issues/5592)) |
| [0046](0046-resources-not-control-flow.md) | reyn bounds resources, not control flow — no guard on workflow recursion; each band member bounds its own resource cause-independently, and the OS owes visibility + a stop instead of a termination guarantee (**Proposed** 2026-09-05, not ratified; extends the #5561 loop-valve abolition; see [#5747](https://github.com/tya5/reyn/issues/5747)) |
| [0039](0039-thin-client-single-writer-server.md) | N thin CUI clients × one single-writer server — UI-path unification, four-surface separation (Proposed) |

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

- Principles (P1–P8) (principles doc removed) — invariants the
  decisions must respect
- Skill resume (page removed) — user-facing summary of
  the resulting machinery
- [Upgrade policy](../../reference/upgrade-policy.md) — operator-facing
  consequence of ADR-0006
