# Feature Proposals

A directory collecting proposals for feature implementation.

ADRs (`decisions/`) record "why a particular design was chosen."
This directory holds proposals for "what should be implemented."

---

## File Naming Convention

```
NNNN-<kebab-case-title>.md
```

Example: `0001-a2a-task-lifecycle.md`

---

## Status Values

| Value | Meaning |
|---|---|
| `proposed` | Proposed, not yet started |
| `accepted` | Implementation approved |
| `in-progress` | Implementation underway (include PR number) |
| `done` | Implementation complete (include commit/PR) |
| `deferred` | On hold (include reason) |
| `rejected` | Rejected (include reason) |

---

## Format

Each proposal file should include the following sections:

```markdown
# FP-NNNN: Title

**Status**: proposed
**Proposed**: YYYY-MM-DD
**Author**: (session name or owner)

## Summary
One paragraph describing what to implement and why.

## Motivation
Use cases, background, comparison with alternatives, etc.

## Proposed implementation
Overview of the implementation approach (detailed design delegated to ADR).

## Dependencies
Prerequisites — other implementations or PRs this depends on.

## Cost estimate
SMALL / MEDIUM / LARGE (with rationale).

## Related
Links to related ADRs, PRs, and docs.
```

---

## Index

| # | Title | Status | Cost |
|---|---|---|---|
| [0001](0001-a2a-task-lifecycle.md) | A2A task lifecycle — ask_user / push notification support | proposed | MEDIUM |
| [0002](0002-index-docs-recall-docs.md) | index_docs / recall_docs — unified document retrieval skill | done (ADR-0033 Accepted, 1e6f153) | LARGE |
| [0003](0003-budget-exceed-user-approval.md) | User approval and resume flow on budget exceed | proposed | SMALL |
| [0004](0004-safety-config-ux.md) | safety config UX improvements — alignment with conceptual layer | proposed | MEDIUM |
| [0005](0005-safety-as-checkpoint.md) | Treat safety limits as checkpoints — integration with Permission model | proposed | LARGE |
| [0006](0006-skill-self-improvement.md) | Skill self-improvement — execution-trace-driven + versioning + rollback | proposed | MEDIUM |
| [0007](0007-evaluation-infrastructure.md) | Agent evaluation infrastructure — P6 trace export + skill regression evaluation | proposed | LARGE |
| [0008](0008-swe-bench-integration.md) | SWE-bench participation infrastructure — stdlib skill + batch execution | proposed | LARGE |
| [0009](0009-operational-intelligence.md) | Operational Intelligence — RAG indexing of event logs | proposed | MEDIUM |
| [0010](0010-rag-routing.md) | RAG routing — semantic pre-filter for skill catalog + routing history | proposed | MEDIUM |
