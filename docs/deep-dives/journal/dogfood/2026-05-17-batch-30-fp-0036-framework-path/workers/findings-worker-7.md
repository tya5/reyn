# B30 Worker 7 Findings — long_session_v1 (7 scenarios)

Date: 2026-05-17
HEAD: 4be42fe (Merge feat/b29-q2-synthetic-event)
Agent: dogfood-b30-7
Run ID: b30-7-long-session

## 4-Band Summary

V/I/R/B = 0/7/0/0 (raw framework output)
True expected: V/I/R/B = 2/5/0/0 (see framework finding below)
Brier: 0.2656

## C1 Verify — Per-Turn Duplicate Tool Check

Total LLM request turns: 52
Turns clean (no duplicates): 52
Turns with duplicates: 0

C1 result: 52/52 turns clean.

Per scenario: all 7 scenarios 0 duplicate-tool turns.

## Multi-Turn Survival

37/37 turns with replies (matches B28). Zero empty replies.

scenario_1: 5/5 (2 inline + 3 routing)
scenario_2: 6/6 (all inline)
scenario_3: 5/5 (all inline)
scenario_4: 6/6 (all inline)
scenario_5: 5/5 (all inline)
scenario_6: 5/5 (4 inline + 1 routing)
scenario_7: 5/5 (4 inline + 1 routing)

## Q2 Event (chat_turn_completed_inline)

32/32 inline turns emitted chat_turn_completed_inline with decision=inline_reply.
5 routing turns emitted routing_decided (not inline). No mismatches.

## B27-7-BUG-2 (reyn__source__read)

Did NOT manifest. 4x source__read calls across S1 and S6, all outcome=success.

## Notable Events

- scenario_3: tool_failed on search_actions (unknown_tool). Agent recovered gracefully, provided comparison from training knowledge.
- scenario_7: web_search invoked once (turn 1, eventual consistency). Succeeded.
- G12 Pattern E: 0 empty stop responses across all 52 request turns.

## Framework Finding

Scenarios 1 and 5 have expected.reply.kind=judge rubrics. Both produced substantive replies satisfying the rubric on manual inspection (S1: 4,903 chars covering P7/workspace/OS-constant; S5: 18,803 chars with complete asyncio.Queue example). Both scored inconclusive because verify_reply is never called by the runner or CLI (pre-existing gap, not a B30 regression). True B30 score = V/I/R/B = 2/5/0/0.

