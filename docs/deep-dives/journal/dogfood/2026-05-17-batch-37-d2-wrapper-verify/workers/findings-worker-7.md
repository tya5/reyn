# B37 Worker 7 — long_session_v1 findings

**Batch**: 37  
**Worker**: 7/7  
**HEAD**: 561101a (D2-wrapper)  
**Date**: 2026-05-17  
**Port**: 8087  
**Agent prefix**: dogfood-b37-7  
**Scenario file**: dogfood/scenarios/long_session_v1.yaml  

## Summary

V/I/R/B = 7/0/0/0  
ΔvsB36 = +0V (matches B36 clean sweep)  
C1 clean turns: 37/37  
D2-wrapper visible: yes (3 alias types observed)  

## Scenario verdicts

| # | Scenario | Verdict | Clean turns | Notes |
|---|----------|---------|-------------|-------|
| 1 | scenario_1_reyn_research_chain | verified | 5/5 | rubric 3/3: P7, context-maintained, non-empty |
| 2 | scenario_2_pronoun_followup | verified | 6/6 | conservative pronoun disambiguation (see §4.1) |
| 3 | scenario_3_cross_reference_compare | verified | 5/5 | T4-T5 tool-limit refusal but non-empty |
| 4 | scenario_4_repetitive_context_bloat | verified | 6/6 | no G12 Pattern E observed |
| 5 | scenario_5_general_python_chain | verified | 5/5 | rubric 3/3: asyncio.Queue example correct |
| 6 | scenario_6_file_and_doc_lookup_chain | verified | 5/5 | ADR not found, graceful fallback |
| 7 | scenario_7_concept_explanation_chain | verified | 5/5 | CAP/eventual consistency chain correct |

## Verification angle results

### Angle 1: C1 multi-turn stability

All 37 turns produced non-empty replies. No G12 Pattern E (empty stop after context growth)
observed. B35=B36=B37 = 37/37 clean (non-regression confirmed).

### Angle 2: D2-wrapper visible across ≥3 alias types

Function `_enrich_invoke_action_description()` is active: `universal_wrappers_enabled=true`
and `hot_list_n=10` in reyn.local.yaml. Direct verification of enriched description:

```
invoke_action description (after _enrich_invoke_action_description):
  ...
  ACTION ARG SCHEMAS (canonical keys for current hot-list actions):
    reyn.source__read: {path}
    web__search: {max_results, query}
    web__fetch: {max_length, url}
    file__read: {path}
    file__write: {content, path}
    reyn.source__list: {path}
  Use these exact key names in args when calling invoke_action.
```

Alias types observed in routing_decided events across 37 turns:

1. **reyn.source__read** — hot_list_alias path: S1 T1, T2, T4, T5; S3 T2
2. **web__search** — hot_list_alias path: S3 T1; invoke_action path: S5 T2, S7 T4
3. **invoke_action** (surface B wrapper) — S1 T3, S5 T2, S7 T4

invoke_action args correctness (B36 was N=3 arg-name mismatches):
- S1 T3: `invoke_action(action="reyn.source__read", args={"path": "README.md"})` — CORRECT (canonical key is "path")
- S5 T2: `invoke_action(action="web__search", args={"query": "..."})` — CORRECT
- S7 T4: `invoke_action(action="web__search", args={"query": "..."})` — CORRECT

**No arg-name mismatches observed in B37 W7 (N=3 invoke_action calls, 0 errors).**
D2-wrapper visible = YES across 3 alias types.

### Angle 3: simple_memo_app attractor

B35 §4.3 concern: agent spontaneously invokes simple_memo_app when it shouldn't.
B36 finding: N=1 non-recur, confounded by skill not being in catalog.

B37 W7 observation:
- `simple_memo_app` exists at `reyn/project/simple_memo_app/skill.md`
- `list_actions(category=["skill"])` returns 20 skills (all `skill__*` prefixed — stdlib only)
- `simple_memo_app` (project skill, no `skill__` prefix) does NOT appear in list_actions results
- No invocation of `simple_memo_app` observed in any of the 37 turns

Hypothesis: B36 finding holds — simple_memo_app attractor is catalog-gap-conditioned.
When the skill is not surfaced by list_actions, the LLM cannot select it. N=0 in B37 W7
(direct observation: 7 scenarios × 37 turns, no tool_called with simple_memo_app).

**simple_memo_app attractor: not manifested in B37 W7.**

### Angle 4: A2A multi-turn history preservation

S2 history.jsonl: 12 entries (6 turn pairs), all sequential with correct seq numbers
(1–12, no gaps). Timestamps confirm proper ordering. History preserved correctly across
all 6 turns of the most pronoun-heavy scenario.

## Notable observations

### 4.1 Pronoun disambiguation behavior (S2)

S2 (pronoun_followup) showed consistent disambiguation-asking behavior for T2–T6:
- T1 listed 32 skills (per agent reply) / 20 skills (per tool_returned items)
- T2 "Tell me more about the simplest one" → agent asked for clarification
- T3–T6 with "it"/"those" → agent repeatedly asked "which skill?"

**Count discrepancy**: Agent said "32 skills" in T1 reply, tool_returned had 20 items.
The LLM hallucinated the count 32 (likely from training prior on skill catalogs). This is
a minor accuracy issue, not a stability issue. All replies were non-empty.

Conservative disambiguation over pronoun resolution is correct behavior (avoids
hallucinating wrong skill details). This reduces pronoun_followup utility as a G12
detector because the agent never attempts to resolve pronouns from history — it deflects.

### 4.2 S6 ADR lookup — expected graceful degradation

S6 (file_and_doc_lookup_chain) prompted about ADRs. Agent found no ADRs via
`list_actions(filter="adr", category=["file"])` and `search_actions(query="ADR")`.
Subsequent turns built a coherent response off the "no ADRs" finding.
History chain preserved: each T after T1 referenced the T1 finding.

### 4.3 S3 T4-T5 — tool-limit refusal

S3 T4 ("which approach handles async tool calls more predictably?") and T5 (LangGraph → Reyn
migration) both returned refusals: "The available tools do not provide information on..."
These were still non-empty and non-attractor (the agent correctly recognized the question
was outside its tool scope). No empty stop.

### 4.4 S5 T2 — "already answered" confusion

S5 T2 ("What is the difference between asyncio.gather and asyncio.wait?") got:
"It looks like you asked a question that was already answered. I'm not sure how to proceed."
The agent confused the follow-up question with a repeat (likely because T1 covered asyncio
basics and the agent's prior-knowledge reply included gather/wait implicitly). Non-empty,
non-attractor, but a mild context-conflation pattern worth noting.

## Event counts

Total routing_decided events: 12 (across all 7 agents)
- hot_list_alias: 9
- invoke_action: 3
- All outcome: success

Total tool_called events: 23 (across all 7 agents)
- list_actions: 6
- search_actions: 5
- reyn.source__read: 4
- web__search: 2
- invoke_action: 3
- describe_action: 1
- other: 2
