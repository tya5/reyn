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
| [0003](0003-budget-exceed-user-approval.md) | User approval and resume flow on budget exceed | done (landed 2026-05-10, commit `2ec46c0`) | SMALL |
| [0004](0004-safety-config-ux.md) | safety config UX improvements — alignment with conceptual layer | done (landed 2026-05-10) | MEDIUM |
| [0005](0005-safety-as-checkpoint.md) | Treat safety limits as checkpoints — integration with Permission model | done (Phase 1+2 landed) | LARGE |
| [0006](0006-skill-self-improvement.md) | Skill self-improvement — execution-trace-driven + versioning + rollback | proposed | MEDIUM |
| [0007](0007-evaluation-infrastructure.md) | Agent evaluation infrastructure — P6 trace export + skill regression evaluation | proposed | LARGE |
| [0008](0008-swe-bench-integration.md) | SWE-bench participation infrastructure — stdlib skill + batch execution | proposed | LARGE |
| [0009](0009-operational-intelligence.md) | Operational Intelligence — RAG indexing of event logs | proposed | MEDIUM |
| [0010](0010-rag-routing.md) | RAG routing — semantic pre-filter for skill catalog + routing history | proposed | MEDIUM |
| [0011](0011-remove-narrator.md) | Remove `skill_narrator` — let the router LLM narrate skill results | done (LANDED 2026-05-10, commit `59c991a`) | SMALL |
| [0012](0012-async-skill-execution.md) | Async skill/agent/plan execution — non-blocking long-running tasks | done (LANDED 2026-05-10, commit `c9e79d6`) | LARGE |
| [0013](0013-unified-inbox-outbox-transport.md) | Unified inbox/outbox transport abstraction — collapse CUI vs MCP/A2A skew | accepted (ADR-A green-light 2026-05-11) | LARGE |
| [0014](0014-python-step-api-package.md) | Python step API package + rename modes (pure→safe, trusted→unsafe) | partial-landed 2026-05-11/13 (A–F + ADR-G Phase 1 + Class B partial; commits `5b435e1`/`b405975`/`527e11f`/`c4b281a`) | MEDIUM |
| [0015](0015-fine-grained-python-step-audit.md) | Fine-grained per-call Python step audit (bidirectional RPC) | deferred (= awaiting enterprise audit requirement) | MEDIUM |
| [0016](0016-agent-authentication.md) | Agent authentication — OAuth delegation, token lifecycle, and MCP auth headers | Component A landed 2026-05-11 (commit `ec94a06`); B/C/D/E proposed | LARGE |
| [0017](0017-sandboxed-execution.md) | Sandboxed execution — policy/backend abstraction and exec op deprecation | Components A+D landed 2026-05-11 (commit `ddf2d05`); B/C/E proposed | MEDIUM |
| [0018](0018-event-store-backend.md) | Event Store backend abstraction — JSONL / SQLite / DuckDB (priority: LOW) | proposed | MEDIUM |
| [0019](0019-chat-session-refactor.md) | ChatSession responsibility separation — extracting services from session.py | partially-landed Waves 1+3 (Compaction+SkillRunner+AutoResume, 2026-05-13/14, `6620505`/`9ae66fa`/`ba7f7c3`); Wave 2 proposed (FP-0013 待ち) | MEDIUM |
| [0020](0020-runtime-layer-decomposition.md) | OSRuntime layer decomposition — splitting runtime.py into vertical layers | done (Components A/B/C/D LANDED 2026-05-13/14, `1dac280`/`5628993`/`7e51216`/`929d81f`; runtime.py 1882→507 LoC) | MEDIUM |
| [0021](0021-event-log-audit-completeness.md) | Event log audit completeness — add run_id/skill to missing events + permission_granted | done (LANDED 2026-05-13, commits `c6f4218`..`a03bcfc`) | SMALL |
| [0022](0022-permission-tier-model.md) | Permission tier model — formalize two-axis framework + fix web_fetch/web_search asymmetry | done (LANDED 2026-05-14, commits `61dc193`/`1f49855` — SSL config follow-up included) | SMALL |
| [0023](0023-router-sp-quick-wins.md) | Router system prompt quick wins — cache efficiency, dedup, spawn-ack priority, delegate_to_agent rule, JA examples | done (LANDED 2026-05-14, commit `45512ba` — 5 edits + FP-0025 D piggyback) | SMALL |
| [0024](0024-router-sp-semantic-tool-selection.md) | Router — semantic tool selection (BM25/embedding pre-filter for invoke_skill enum) | partial-landed Component D (Anthropic tool_search_tool MCP, 2026-05-14, `aa1b36f`); A/B/C deferred (YAGNI at current scale) | MEDIUM |
| [0025](0025-planner-narration-and-sp-fixes.md) | Planner — router narration (align with skill/FP-0012) + plan step SP fixes | done (LANDED 2026-05-14, commits `6da92fe`/`45512ba`/`635ce55` — A+B+C+D all landed) | SMALL |
