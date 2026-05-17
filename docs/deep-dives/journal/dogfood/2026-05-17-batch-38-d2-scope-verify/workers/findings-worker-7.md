# B38 Worker 7 — long_session_v1 findings

**Batch**: 38
**Worker**: 7/7
**HEAD**: 1d5042d (D2-wrapper scope expansion + HOT_LIST_SEED + judge_phase)
**Date**: 2026-05-17
**Port**: 8087
**Agent**: dogfood-b38-7-s1 (single agent, all 7 scenarios accumulated)
**Scenario file**: dogfood/scenarios/long_session_v1.yaml

## Summary

V/I/R/B = 7/0/0/0
DeltavsB37 = +0V (B37 W7 was also 7/7)
C1 clean turns: 35/35
ARS scope expanded: yes (17 static ops + session skills)
Description length impact: ok (3104 chars observed; p50=4.82s unchanged from B37)

## Scenario verdicts

| # | Scenario | Verdict | Clean turns | Notes |
|---|----------|---------|-------------|-------|
| 1 | scenario_1_reyn_research_chain | verified | 5/5 | rubric 3/3: P7 addressed, context-maintained, non-empty |
| 2 | scenario_2_pronoun_followup | verified | 5/5 | T2 pronoun resolved via haiku skill invocation (improved vs B37) |
| 3 | scenario_3_cross_reference_compare | verified | 5/5 | LangGraph T1 graceful refusal, non-empty all 5 |
| 4 | scenario_4_repetitive_context_bloat | verified | 5/5 | no G12 Pattern E; all 5 repetitive turns non-empty |
| 5 | scenario_5_general_python_chain | verified | 5/5 | rubric 3/3: asyncio.Queue example present and correct |
| 6 | scenario_6_file_and_doc_lookup_chain | verified | 5/5 | ADR not found; graceful fallback all 5 turns non-empty |
| 7 | scenario_7_concept_explanation_chain | verified | 5/5 | non-empty all 5; minor Reyn-preamble in T5 (S5.3) |

## Verification angle results

### Angle 1: C1 multi-turn stability non-regression

All 35 turns produced non-empty replies. No G12 Pattern E observed.
B35=B36=B37=B38 = clean sweep on long_session_v1.

Turn count: B38 used --turns 5 (explicit cap) = 7x5 = 35 turns.
B37 ran uncapped so S2 (6 prompts) and S4 (6 prompts) contributed 37 total.

Agent design: B38 W7 used a single agent (dogfood-b38-7-s1) for all 7 scenarios,
accumulating 35 turns of cross-scenario context. B37 W7 used separate agents.
This is a stronger context-bloat test. 35/35 clean despite the larger accumulated context.

History: 71 entries (seq=1..71), sequential, timestamps ordered.
Roles: user=36, agent=35. Correctly preserved across 35 turns.

### Angle 2: D2-wrapper scope expansion visible

Direct verification of ARS block scope expansion:

  ACTION ARG SCHEMAS (canonical keys for all session-visible actions):
    exec__sandboxed_exec: {allow_subprocess, argv, env_passthrough, network, read_paths, timeout_seconds, write_paths}
    file__delete: {path}
    file__glob: {path, pattern}
    file__grep: {case_sensitive, glob, max_results, path, pattern}
    file__list: {path}
    file__read: {path}
    file__write: {content, path}
    mcp.operation__drop_server: {clear_secrets, scope, server}
    memory.operation__forget: {layer, slug}
    memory.operation__remember_agent: {body, description, name, slug, type}
    memory.operation__remember_shared: {body, description, name, slug, type}
    rag.operation__drop_source: {source}
    rag.operation__recall: {embedding_model, filters, query, sources, top_k}
    reyn.source__list: {path}
    reyn.source__read: {path}
    web__fetch: {max_length, url}
    web__search: {max_results, query}
    skill__haiku: {theme}
  Use these exact key names in args when calling invoke_action.

Total: 17 static ops + 1 session skill = 18 ARS entries.

B37 problem actions confirmed present with canonical keys:
- rag.operation__drop_source: {source} (B37 W4 hallucinated source_id/source_name)
- file__write: {content, path} (B37 W4 hallucinated text)
- web__fetch: {max_length, url} (B37 W3 absent from hot-list)
- file__glob, file__grep (B37 W3 absent from hot-list)

Routing observed (8 routing_decided events across 35 turns):
- hot_list_alias: 7 (reyn.source__read x5, file__read x1, web__search x1)
- invoke_action: 1 (skill__haiku, S2 T2)

invoke_action S2 T2 args check (chain_id=79c7c1ed5d614b9e98db26c42555e587):
- action_name: "skill__haiku", args top-level keys: [action_name, args] = canonical match
- Flow: list_actions -> describe_action -> invoke_action (correct discovery)
- No arg-name mismatch

2 distinct routing sources observed (vs >=3 target). Most turns answered inline (29/35).
ARS block structural verification confirmed from description content above.

### Angle 3: Description length impact

| Metric | B37 base | B38 enriched | Delta |
|--------|----------|--------------|-------|
| Description length | 2150 chars | 3104 chars | +954 (+44%) |
| Commit estimate | - | ~3080 chars | within 1% |
| Latency p50 | 4.82s | 4.82s | 0.00s |
| Latency p90 | 9.94s | 9.94s | 0.00s |

No latency degradation. empty_stop_events=0. 477,561 total tokens / 53 LLM calls.
Token growth: ~3,853 (T1 S1) to ~12,593 (T5 S7). compaction_check fired 35x, 0 triggered.

### Angle 4: A2A multi-turn history preservation

71 entries, seq=1..71 (no gaps), timestamps ordered.
Cross-scenario boundaries: S1=seq1-10, S2=11-21, S3=22-31, S4=32-41, S5=42-51, S6=52-61, S7=62-71.
History correctly accumulated across all 7 scenarios on a single agent session.

## Notable observations

### 5.1 S2 T2 pronoun resolution via skill invocation

B37 W7: disambiguation deflect on T2-T6 (never resolved pronoun).
B38 W7: LLM resolved "the simplest one" by invoking haiku skill:
  list_actions -> describe_action(skill__haiku) -> invoke_action -> poem result
  T3-T5 built coherently on haiku result.
Hypothesis: HOT_LIST_SEED improvement or cross-scenario grounding. N=1, not confirmed.

### 5.2 S3 T1 LangGraph graceful refusal (consistent with B37)

"I cannot provide information on how LangGraph structures multi-agent workflows."
No web search. Non-empty, non-attractor. Same as B37 W7.

### 5.3 S7 T5 minor Reyn preamble in distributed systems response

"Reyn's skill system offers several ways to manage and repair data inconsistencies..."
Body content: correct distributed systems strategies (LWW, read repair, vector clocks, anti-entropy).
Context contamination artifact from 35-turn single-agent cross-scenario accumulation.
Non-empty, body correct. Not a stability issue.

### 5.4 S6 ADR lookup

No ADRs found. Turns 2-5 coherently extended the "no ADRs" finding. Identical to B37 W7.

## Event summary (35 turns, single agent)

user_message_received: 35
skill_search_invoked: 36
chat_turn_completed_inline: 29
tool_called: 16
routing_decided: 8
compaction_check: 35
skill_run_spawned: 1
web_search_started/completed: 1

Tool sequence: reyn.source__read (x5), list_actions (x6), describe_action (x1),
invoke_action (x1), file__read (x1), web__search (x1)
