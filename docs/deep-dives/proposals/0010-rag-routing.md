# FP-0010: RAG Routing — Semantic Pre-filter for Skill Catalog + Routing History

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Before calling the LLM for a user request, the OS executes `recall` at the OS level
to narrow relevant skill candidates down to a top-K list and injects them into the
system prompt as a "Suggested for this request" section. This keeps the number of
candidates the LLM sees constant even as the skill count grows.
If no index has been built, the section is skipped and behavior falls back to the existing flow.

---

## Motivation

### Resilience to Growing Tool Count

The current router presents 14–23 tools in a flat list.
As FP-0006 through FP-0009 are implemented, the number of skill types will grow further,
degrading the LLM's selection accuracy.

```
Current:   LLM → selects from a full skill list (impractical at 100 skills)
This FP:   OS narrows to top-K via recall → LLM selects from the narrowed candidates
```

### Layer 2: Gets Smarter with Use

As routing history (`routing_decided` events) accumulates, "skills that succeeded for
similar past requests" can be used as few-shot hints.
The foundation from FP-0009 (Operational Intelligence) grows this automatically.

---

## Core Design

### recall as OS Pre-processing, Not an LLM Tool

```
[Rejected approach A]
  User input → LLM → recall tool call → sees result → invoke_skill
  Problem: One tool-call latency per turn. The LLM may misjudge whether to call recall.

[Adopted approach B]
  User input → [OS] recall executed → top-K injected into SP → LLM sees candidates from the start
  Advantage: No extra turn. If no index exists, simply skip the section.
```

This is symmetric with how the `indexed_sources` section is injected before SP construction
in `router_loop.py`.

### No Index → Skip (Graceful Degradation)

```python
# router_loop.py
routing_hints = await recall_for_routing(user_input)  # None if no index
system_prompt = build_system_prompt(
    ...,
    routing_hints=routing_hints,  # None → no section (existing behavior preserved)
)
```

### P4 Compliance

The recall results are **hints only**. The final constraint is the `invoke_skill` enum
(the candidate set provided by the OS).
It is also correct behavior for the LLM to ignore the hints and use `list_skills`.

---

## Proposed implementation

### Component A — OS-Level recall Pre-filter (SMALL)

**Changes to `src/reyn/chat/router_loop.py`:**

```python
async def _build_routing_hints(user_input: str) -> RoutingHints | None:
    """
    Execute recall against skill_catalog and routing_history.
    Returns None (skip) if neither index exists.
    """
    manifest = get_source_manifest(Path.cwd())
    if not manifest.has_source("skill_catalog") \
       and not manifest.has_source("routing_history"):
        return None

    results = await recall_op(
        query=user_input,
        sources=["skill_catalog", "routing_history"],
        top_k=5,
        filter={"outcome": "success"},   # Layer 2: successful routes only
    )
    return RoutingHints(results=results)
```

`recall_op` uses the existing `src/reyn/op_runtime/recall.py` as-is.
The only new OS change is this call wrapper.

### Component B — `routing_decided` P6 Event (SMALL)

Emitted when the router executes `invoke_skill`.
Becomes the knowledge base for Layer 2.

```python
# router_loop.py — inside the invoke_skill tool handler
event_log.emit("routing_decided",
    user_input=user_input,               # User's natural-language input
    chosen_skill=skill_name,             # The skill that was selected
    top_k_considered=[r.name for r in routing_hints.results] if routing_hints else [],
    routing_source=routing_source,       # "rag_hint" | "list_skills" | "explicit"
    outcome=None,                        # Updated to "success" / "wrong_skill" after execution
)
```

The `outcome` field is updated by reconciling with `run_skill_completed` after skill execution.
"User re-runs a different skill in the same turn → detected as wrong_skill."

### Component C — "Suggested for this request" Section (SMALL)

**Changes to `src/reyn/chat/router_system_prompt.py`:**

```
## Suggested for this request
The most relevant skills for your request:

1. **swe_bench** — Solve a SWE-bench task (from skill catalog)
2. **code_review** — Review code (from similar past requests)

You can invoke these directly with invoke_skill.
If you need a different skill, use list_skills to see the full list.
```

`routing_hints` is None (no index) → the entire section is omitted.
`routing_hints` is empty (index exists but no hits) → section is omitted (to avoid confusion).

Insertion position: immediately before the `## Skills` section (most prominent position).

### Component D — Indexing the Skill Catalog (SMALL)

Add a command to build the `skill_catalog` source with `index_docs`.

```
reyn run index_docs --source skill_catalog
```

Chunk design for skill.md (structured, unlike documents):

```
[skill chunk: swe_bench]
name: swe_bench
description: Solve a SWE-bench task — code fix and verification for a GitHub issue
tags: coding, benchmark, github, testing
input: repository URL, issue description, test patch
```

**Auto-update trigger (future):** A hook to re-index `skill_catalog` when a skill is added or changed.
Manual execution for now.

#### Optional: `example_phrases` Field

An optional field added to skill.md frontmatter. Skill authors can use it to tune semantic matching.

```yaml
# skill.md frontmatter
example_phrases:
  - "fix the bug"
  - "make the tests pass"
  - "fix the code in the pull request"
```

`index_docs` includes this field in the chunk (at the skill author's discretion).

### Component E — Indexing the Routing History (SMALL)

Add processing for `routing_decided` events to FP-0009's `index_events`.

```
[routing history chunk]
user_input: "fix the bug in django"
chosen_skill: swe_bench
routing_source: explicit
outcome: success
timestamp: 2026-05-10T09:15:00
```

Filtering: index only entries where `outcome == "success"`.
Failed or corrected routes are excluded (to prevent incorrect few-shots).

---

## Phased Implementation

| Phase | Content | Prerequisites |
|---|---|---|
| **Phase 1** | Components A–D (Layer 1: skill catalog) | ADR-0033 RAG ✅ |
| **Phase 2** | Component B `outcome` update + Component E (Layer 2: routing history) | FP-0009 |

Phase 1 alone is valuable as "skill catalog semantic routing."
Phase 2 is added after FP-0009's `index_events` has matured.

---

## Full Flow Diagram

```
User input
    ↓
[OS] recall(sources=["skill_catalog", "routing_history"], filter={outcome:success})
    ├─ No index → None (skip)
    └─ Index exists → top-5 candidates
    ↓
[OS] build_system_prompt(routing_hints=top_5)
    → Inject "## Suggested for this request" section (or omit)
    ↓
[LLM] invoke_skill (enum constraint) or list_skills, referring to hints
    ↓
[P6] routing_decided event emitted (user_input / chosen_skill / routing_source)
    ↓
After skill execution completes → outcome updated (success / wrong_skill)
    ↓
[FP-0009] index_events periodically indexes routing_history
    → Layer 2 self-grows
```

---

## Dependencies

- ADR-0033 RAG Phase 1 (✅ landed) — `recall` op / SourceManifest are prerequisites
- `src/reyn/chat/router_loop.py` — Components A / B (recall pre-filter + event emit)
- `src/reyn/chat/router_system_prompt.py` — Component C (add section)
- `src/reyn/op_runtime/recall.py` — no changes (existing recall op used as-is)
- FP-0009 (Operational Intelligence) — Component E's `routing_decided` processing (Phase 2 only)

No prerequisite PRs. Phase 1 can be implemented independently without FP-0009.

---

## Cost estimate

**Total: MEDIUM**

| Task | Cost | Notes |
|---|---|---|
| Component A: recall pre-filter (router_loop.py) | SMALL | recall_op call wrapper |
| Component B: routing_decided event + outcome update | SMALL | emit in 1 place + reconciliation with run_skill_completed |
| Component C: inject "Suggested" section into SP | SMALL | Add section to router_system_prompt.py |
| Component D: skill_catalog indexing + chunk design | SMALL | Add source config to index_docs |
| Component E: routing_history indexing (Phase 2) | SMALL | Extension of FP-0009 index_events |
| Tests (Tier 2: router invariant) | SMALL | Contract test for skip behavior when no index exists |

Bottleneck is **Component B's outcome update** (the reconciliation logic between
post-skill-execution success/failure and routing_decided).

---

## Related

- `src/reyn/chat/router_loop.py` — recall pre-filter insertion point
- `src/reyn/chat/router_system_prompt.py` — add "Suggested" section
- `src/reyn/op_runtime/recall.py` — existing recall op (no changes)
- `src/reyn/index/source_manifest.py` — has_source() for existence check
- ADR-0033 (`docs/deep-dives/decisions/0033-rag-extensible-os.md`) — RAG foundation
- FP-0009 (`0009-operational-intelligence.md`) — self-growing foundation for routing_history
