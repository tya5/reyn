# B28 Worker 7 — long_session_v1 Findings

**Batch**: B28 Worker 7  
**Date**: 2026-05-17  
**HEAD**: f5a6866  
**Scenarios**: 7 (long_session_v1.yaml)  
**Primary focus**: B27-C1 fix verification

## C1 Verify — Per-Turn Duplicate Check

**C1 verdict: ALL CLEAN — 62 LLM calls, 0 duplicates.**

| Scenario | LLM Call # | Duplicate? | If yes: which wrapper |
|---|---|---|---|
| scenario_1 | 1-5 | no | — |
| scenario_2 | 1-17 | no | — |
| scenario_3 | 1-9 | no | — |
| scenario_4 | 1-9 | no | — |
| scenario_5 | 1-8 | no | — |
| scenario_6 | 1-8 | no | — |
| scenario_7 | 1-6 | no | — |

Wrapper tools (list_actions, describe_action, invoke_action) appear exactly once per LLM call in all 62 calls. search_actions absent (consistent — no eager-embedding-build flag).

## Multi-Turn Survival

All 7 scenarios completed all planned turns: 37/37 turns (100%). B27 baseline: 4/37 (~11%) due to C1 crash. The B27-C1 fix is verified effective.

## Scenario Scores

- scenario_1: VERIFIED (5/5 turns, rubric pass: P7 principles addressed, context carried, non-empty final)
- scenario_2: INCONCLUSIVE (6/6 turns, no rubric; skill errors recovered cleanly)
- scenario_3: INCONCLUSIVE (5/5 turns, no rubric; T3/T4 refusal observed)
- scenario_4: INCONCLUSIVE (6/6 turns, no rubric; no G12 attractor)
- scenario_5: VERIFIED (5/5 turns, rubric pass: asyncio.Queue producer-consumer code in final reply)
- scenario_6: INCONCLUSIVE (5/5 turns, no rubric; ADR catalog miss)
- scenario_7: INCONCLUSIVE (5/5 turns, no rubric; all turns substantive)

V/I/R/B = 2/5/0/0

## B27-7-BUG-2

Not observed across all 7 scenarios (no reyn__source__read hallucination).

## Additional Observations

1. Skill error recovery works (writing_review_app, index_events failures — agent narrated and continued, no crash).
2. Scenario 3 T3/T4: LLM self-limited to "I am a Reyn agent" refusing asyncio questions. Over-constraint behavioral attractor.
3. Scenario 6: ADRs exist at docs/deep-dives/decisions/ but reyn.source__list did not surface them. Catalog seed coverage gap.
4. Tool count varies 12-14 due to hot-list mechanism (dynamic skill add/remove). No duplicates.
