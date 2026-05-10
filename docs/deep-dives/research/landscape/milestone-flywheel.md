---
title: "Milestone: Reyn Flywheel — Self-Improvement and Auditability Together"
last_updated: 2026-05-10
status: vision
---

# Milestone: Reyn Flywheel — Self-Improvement and Auditability Together

> **"Gets smarter with every use. But it can always explain what it learned and how."**

---

## What This Milestone Means

A survey of the competitive AI agent framework landscape (2026-05) confirmed:
**no product ships self-improvement and auditability at the same time.**

```
Hermes GEPA:   self-improvement ✅ / auditable ❌  (EU AI Act violation flagged in Issue #17619)
LangSmith:     self-improvement ❌ / auditable △   (observation only)
Others:        one or the other, never both
```

When this milestone is reached, Reyn becomes the first framework to solve this
problem at production grade — a problem no one has solved yet.

---

## Why This Is Structurally Hard

Self-improvement and auditability are normally in tension:

```
When you pursue self-improvement
  → the LLM rewrites the system
  → tracking what changed becomes difficult
  → auditability degrades

When you preserve auditability
  → changes require approval gates
  → self-improvement stalls or slows
```

The reason Reyn can achieve both is that the architectural foundations —
the **P6 event log + Permission model** — were in place first.

- Skill changes go through `write_file` op → recorded in P6 (audit)
- `write_file` passes through a Permission check (control)
- WAL tracks before and after each change (rollback)

Because self-improvement runs as "ordinary OS execution," an improvement trail
is created automatically.

---

## Structure of the Flywheel

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│   Use it → Record it → Index it                     │
│        ↑                    │                        │
│        └──── Next run improves ◄──────┘              │
│                                                     │
└─────────────────────────────────────────────────────┘
```

Properties of the flywheel:
- **Starts at baseline quality** — quality must be adequate from the start (poor quality reverses the wheel)
- **Accelerates with continued use** (both routing accuracy and skill quality improve)
- **Every change is recorded in P6** (what was learned and how is always explainable)

---

## Component FPs and Dependencies

```
[Foundation — complete]
  ADR-0033 RAG Phase 1 ✅
    embed / index_write / recall / index_query ops
    index_docs skill
    SqliteIndexBackend / SourceManifest

[Layer 1 — relatively achievable]
  FP-0009 Operational Intelligence
    index_events skill (event log → knowledge base)
    ↓ prerequisite for the following
  FP-0007 Evaluation Infrastructure
    P6 export adapter / reyn eval CLI
  FP-0010 RAG Routing Phase 1
    semantic pre-filter for the skill catalog

[Layer 2 — model quality dependent]
  FP-0006 Skill Self-Improvement
    collect_traces → failure analysis → plan_improvements
    ← requires "model strength sufficient to accurately analyze failures"
  FP-0010 RAG Routing Phase 2
    learning from routing_decided history
    ← after FP-0009 has matured
  FP-0008 SWE-bench
    code modification and verification loop
    ← requires frontier-equivalent model

[Flywheel completion condition]
  FP-0009 + FP-0006 + FP-0010 Phase 2 all in place
```

---

## An Honest Assessment of the Current State

| Item | Status | Notes |
|---|---|---|
| Correctness of the design | ✅ confirmed | from competitive research and architecture analysis |
| Foundation infrastructure | ✅ implemented | P6 + RAG Phase 1 |
| FP-0009–0010 Phase 1 | 🔧 designed, not yet implemented | relatively achievable |
| FP-0006 self-improvement quality | ⚠️ uncertain | depends on model strength |
| FP-0008 SWE-bench | ⚠️ uncertain | difficult with flash-lite |
| Flywheel end-to-end | 🔭 unvalidated | depends on all components reaching sufficient quality |

**At this point the flywheel is a design on paper.** The foundations are real, but whether
it will spin as a flywheel depends on accumulated quality from the model and end-to-end layers.

---

## Completion Criteria

The "flywheel milestone achieved" verdict is declared when all of the following hold:

1. `index_events` indexes the P6 log and it is searchable via `recall`
2. `reyn eval compare` outputs regression comparisons between skill versions
3. RAG routing presents top-K results from the skill catalog and measurably improves actual routing accuracy
4. `skill_improver` generates improvement proposals from past failure traces and scores improve
5. All of these changes are recorded in P6 and trackable via `routing_decided` / `skill_improved` / `skill_rolled_back` events

---

## What This Milestone Opens

When the flywheel starts turning, Reyn shifts from something you build with
to something that grows with you.

**As an OSS launch message:**

> "The longer you use Reyn, the more it optimizes for your organization's workflows.
>  And because everything it learns is recorded, you can inspect or roll back any change,
>  at any time."

This is a positioning that simultaneously satisfies the "controllable AI" that Japanese
enterprises demand and the "AI that gets smarter" that global markets expect.

---

## Related Documents

- `docs/deep-dives/proposals/0006-skill-self-improvement.md`
- `docs/deep-dives/proposals/0007-evaluation-infrastructure.md`
- `docs/deep-dives/proposals/0008-swe-bench-integration.md`
- `docs/deep-dives/proposals/0009-operational-intelligence.md`
- `docs/deep-dives/proposals/0010-rag-routing.md`
- `docs/deep-dives/research/competitive/hermes-agent.md`
- `docs/deep-dives/research/landscape/reyn-strategic-priorities.md`
