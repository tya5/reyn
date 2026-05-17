# B39 Worker 7 — long_session_v1 findings

**Batch**: 39
**Worker**: 7/7
**HEAD**: b4daeb1 (fix(stdlib): explicit input_schema for empty-schema skills)
**Date**: 2026-05-17
**Port**: 8087
**Agent**: dogfood-b39-7-s1 (single agent, all 7 scenarios accumulated)
**Scenario file**: dogfood/scenarios/long_session_v1.yaml

## Summary

V/I/R/B = 7/0/0/0
DeltavsB38 = +0V (B38 W7 also 7/7)
C1 clean turns: 35/35
Description length (full): B38=8305 chars, B39=~9578 chars (+1273, +15.3%)
Description length (ARS static section): B38=3104 chars, B39=~3168 chars (+64, +2%)
Latency p50: 3.56s (B38: 4.82s, delta=-1.26s=-26.1%) — partially confounded by S7
Latency p90: 6.79s (B38: 9.94s, delta=-3.15s=-31.7%) — partially confounded by S7

## Scenario verdicts

| # | Scenario | Verdict | Clean turns | Notes |
|---|----------|---------|-------------|-------|
| 1 | scenario_1_reyn_research_chain | verified | 5/5 | rubric 3/3: P7 addressed, context maintained, non-empty |
| 2 | scenario_2_pronoun_followup | verified | 5/5 | pronoun resolved to skill__direct_llm; invoke_action(memory.operation__remember_shared) at T4 |
| 3 | scenario_3_cross_reference_compare | verified | 5/5 | LangGraph graceful refusal T1,T4,T5; Reyn/async answered T2,T3 |
| 4 | scenario_4_repetitive_context_bloat | verified | 5/5 | no G12 Pattern E; all 5 repetitive turns non-empty |
| 5 | scenario_5_general_python_chain | verified | 5/5 | rubric 3/3: asyncio.Queue example present and correct |
| 6 | scenario_6_file_and_doc_lookup_chain | verified | 5/5 | ADR not found; graceful fallback all 5 turns non-empty |
| 7 | scenario_7_concept_explanation_chain | verified | 5/5 | non-empty all 5; content is search-failure apology (quality regression vs B38) |

## Verification angle results

### Angle 1: C1 multi-turn stability non-regression

All 35 turns produced non-empty replies. No G12 Pattern E observed.
B35=B36=B37=B38=B39 = clean sweep on long_session_v1 (35/35 each, --turns 5 cap).

History: 70 entries (seq=1..70), sequential, timestamps ordered.
Roles: user=35, agent=35. Correctly preserved.

Empty by turn position: 0/7 for all turn positions 1-5.

### Angle 2: Description length impact

#### Description length trajectory

| Metric | B37 | B38 | B39 |
|--------|-----|-----|-----|
| Full description (chars) | unknown | 8305 | ~9578 |
| ARS static section (chars) | 2150 | 3104 | ~3168 |
| ARS static entry count | 18 | 18 | 27 (17ops+10stdlib) |
| Latency p50 | 4.82s | 4.82s | 3.56s |
| Latency p90 | 9.94s | 9.94s | 6.79s |

Source: B39 W3 LLM trace (llm_trace_b39_w3.jsonl) = 9554 chars. W7 session adds dogfood-b39-7-s1
to peer list = +~24 chars = ~9578 total.

Key structural change: ARS static entry count 18 -> 27 (+9). Entries added: skill__direct_llm,
skill__read_local_files (the 2 new ARS entries from B39 fix wave #120). Also: fresh reset means
no session skills (B38 W7 had skill__haiku as session skill); stdlib skills filled to 10.

ARS section char delta (+64) is small. Large total delta (+1273) driven by agent.peer list growth.

#### Latency analysis

p50=-1.26s (-26%), p90=-3.15s (-32%). IMPROVEMENT, but partially confounded:
S7 behavioral change (search-failure shortcut) reduces S7 response time vs B38 W7 full answers.
For S1-S6, latency is consistent with B38 given model variance. No description-induced regression.

### Angle 3: G31 epsilon-2 / invoke_action correctness

S2 T4: invoke_action(memory.operation__remember_shared) called correctly.
Routing source=invoke_action, outcome=success. No arg-name mismatch.
2 new ARS entries (skill__direct_llm, skill__read_local_files) caused no confusion.

Routing decisions (5 total):
- hot_list_alias: 3 (reyn.source__read x3)
- invoke_action: 2 (reyn.source__read x1 T1, memory.operation__remember_shared x1 S2)

### Angle 4: state_mode = fresh

dogfood_fresh_reset.sh ran before session. No prior action_usage.jsonl.
State mode: fresh.

## Notable observations

### 5.1 S7 search-failure pattern (behavioral quality regression, N=1)

B38 W7 S7: distributed systems questions answered from general knowledge (substantive responses).
B39 W7 S7: all 5 turns used search_actions -> no results -> apology response:
  "I apologize, but I was unable to find any information about eventual consistency
  in distributed systems using the available tools."

Reply lengths: 130, 102, 138, 130, 126 chars (vs B38 substantive answers).
Events: T1-T3 each called search_actions once; T4-T5 had no tool calls.

Hypothesis A: context accumulation at turn 61-70 (max context state) shifts LLM toward
tool-lookup before general knowledge answers. N=1, unconfirmed.
Hypothesis B: skill__direct_llm in ARS ("single-shot natural language tasks") signals to LLM
to try tools first for any natural language task. N=1, unconfirmed.

NOT a C1 issue (non-empty). IS a content quality regression. Latency improvement partially
confounded: S7 search-failure path is faster than substantive answer path.

### 5.2 S2 pronoun resolution changed (non-blocking)

B38 W7: "simplest one" resolved to skill__haiku (invoked, B38 had haiku as session skill).
B39 W7: "simplest one" resolved to skill__direct_llm (described at T2); T4 invoked
memory.operation__remember_shared. Fresh reset removes session skills; skill__direct_llm
is now the simplest ARS skill.

### 5.3 S6 ADR lookup (identical to B38)

No ADRs found. All 5 turns gracefully extended the "no ADRs" finding. T5 gave substantive
next-ADR suggestions (len=599).

### 5.4 S1 rubric verification (3/3)

S1 T5 (seq=10, len=2010) explicitly addresses: Constrained Decision-Making (P4), Separation of
Concerns (P1/P2), Workspace truth (P5), Immutable Event Log (P6), Artifact Validation (P8).
Context maintained across 5 turns without re-explaining base concepts. Rubric pass.

### 5.5 S5 rubric verification (3/3)

S5 T5 (seq=50, len=6112) contains working asyncio.Queue producer-consumer with asyncio.sleep,
queue.put, queue.get, queue.task_done, asyncio.gather, asyncio.create_task. Rubric pass.

## Event summary

user_message_received: 35
chat_turn_completed_inline: 30
tool_called: 20
tool_returned: 20
routing_decided: 5
compaction_check: 35
permission_granted: 2
workspace_updated: 1

Tool sequence: invoke_action(x2), reyn.source__read(x4), list_actions(x5),
describe_action(x4), search_actions(x5)

5 search_actions calls concentrated in S7 (T1-T3) and late scenarios (S4 area, S6).

## Totals

Total tokens: 437,063 (55 LLM calls)
Token range: 4,141 (S1 T1) to 12,684 (S1 T5 reyn.source__read chain)
