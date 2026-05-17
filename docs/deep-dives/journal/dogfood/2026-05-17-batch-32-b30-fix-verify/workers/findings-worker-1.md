# B32 Worker 1 Findings — chat_router_smoke (7 scenarios)

**Date**: 2026-05-17
**HEAD**: c8fae2e (feat/fp-0034-phase1-universal-catalog)
**Agent**: dogfood-b32-1
**Retest of**: B30 Worker 1 (V/I/R/B = 0/1/6/0)

---

## Verdict Matrix

| Scenario | B28 | B30 | B32 | Notes |
|---|---|---|---|---|
| simple_capability_question | — | I | V | Inline reply, skills mentioned |
| factual_query_direct_llm | — | R | V | Correct idempotency explanation inline |
| skill_discovery_request | — | R | R | routing_decided not emitted (list_actions inline path) |
| explicit_skill_invocation_word_stats | — | R | V | Full skill lifecycle verified |
| catalog_routing_decided_emitted | — | R | R | WAL contamination from S4 |
| multi_turn_pronoun_reference | — | R | V | Multi-turn coherent, code examples present |
| out_of_scope_graceful_decline | — | R | R | Agent asked for image details, did not decline |

**B32 V/I/R/B = 4/0/3/0 | Delta vs B30 = +4V**

---

## S1: simple_capability_question — VERIFIED

Events: user_message_received + chat_turn_completed_inline emitted. No permission_denied.
Reply listed list_actions, invoke_action, file__read, skill__*, memory.operation__remember_shared.
Rubric: mentions skills and capabilities — PASS.

NEW-1: 17 tools in first request including skill__index_docs + skill__eval — CONFIRMED.

---

## S2: factual_query_direct_llm — VERIFIED

Events: user_message_received + chat_turn_completed_inline. No permission_denied.
Reply correctly explained idempotency with HTTP method examples (GET, PUT, DELETE).
Rubric: factual and comprehensible explanation — PASS.

---

## S3: skill_discovery_request — REFUTED

Events: user_message_received → tool_called(list_actions, category=["skill"]) → tool_returned → chat_turn_completed_inline.
routing_decided NOT emitted — must_emit FAIL.
Reply listed all 16 skills with descriptions (rubric content PASS, but structural requirement fails).

Root cause: Router chose list_actions inline path, not skill routing. No routing_decided event for this path.
NEW-3: list_actions returned 16 skills with no list_comprehension_generator or carry-over entries — CLEAN.

---

## S4: explicit_skill_invocation_word_stats — VERIFIED

Events (chat): user_message_received → tool_called → skill_run_spawned → tool_returned → routing_decided → skill_run_completed(finished)
Events (skill_run): full workflow lifecycle from workflow_started to workflow_finished including python_step_started/completed.

All must_emit satisfied: routing_decided, skill_run_spawned, skill_run_completed(success). No permission_denied.
Reply: "単語数: 9, ユニーク単語数: 8, 最も頻繁: 'the' (2回)" — PASS.

---

## S5: catalog_routing_decided_emitted — REFUTED

Events: session_restored → skill_completion_injected(finished) → chat_turn_completed_inline → user_message_received → chat_turn_completed_inline

WAL contamination: S4's word_stats_demo pending completion was in wal.jsonl (not wiped) and was replayed at S5 session start.
Agent's first output: "The `word_stats_demo` skill has finished successfully..." — not a poem.
Trace inspection: S5 messages array contained S1's user+assistant turns (history.jsonl not wiped).

chat_turn_completed_inline was emitted (x2) — must_emit_any satisfied structurally.
But reply content fails rubric (no poem produced in visible output).

**B32-NEW-FINDING-1 (WAL contamination)**: .reyn/state/wal.jsonl is not in wipe recipe. Pending skill completions from prior scenarios are replayed into next scenario via session_restored.

**B32-NEW-FINDING-2 (History contamination)**: .reyn/agents/dogfood-b32-1/history.jsonl is not in wipe recipe. Prior conversation turns accumulate across scenarios. Confirmed via trace inspection showing S1 messages in S5 context.

Recommended wipe additions:
  rm -f .reyn/state/wal.jsonl
  truncate -s 0 .reyn/agents/dogfood-b32-1/history.jsonl

---

## S6: multi_turn_pronoun_reference — VERIFIED

Events: two user_message_received + two chat_turn_completed_inline. No permission_denied.
Note: session_restored absent despite history.jsonl having prior turns — S6's topic was different, no pending completions in WAL post-S5.

Turn 1 reply: concise list comprehension explanation.
Turn 2 reply: 3 Python examples (squares, even_squares, uppercase_words) demonstrating list comprehension coherent with Turn 1.
Rubric: PASS.

---

## S7: out_of_scope_graceful_decline — REFUTED

Events: user_message_received + chat_turn_completed_inline. No permission_denied.
Reply: "どのような画像を生成したいですか？画像の内容、スタイル、色調など..." — asked for clarification on image details.

Rubric 1: "politely declines or explains capability not available" — FAIL (agent did not decline).
Rubric 2: "does not pretend to generate" — PASS.

Matches B30 behavior. Agent treats image generation as an actionable request and asks for specs.

---

## NEW-1 Verify: Cold-start hot list visibility

Direct inspection (all 7 traces via dogfood_trace.py --mode llm-tools-schema):
Every first LLM request contained 17 tools including skill__eval AND skill__index_docs.
5Q pre-conclusion check: 7/7 directly inspected, primary data (LLM payload), no falsifying cases.

VERDICT: NEW-1 confirmed. B30-NEW-1 (hot_list_n default 10→16) successfully surfaces both new seed entries.

---

## NEW-3 Verify: reyn/local/ isolation

list_actions(category=["skill"]) in S3 returned 16 entries — no list_comprehension_generator or unexpected entries.
reyn/local/ path: clean across all scenarios (wipe recipe cleared it correctly).

VERDICT: NEW-3 (reyn/local/ scope) confirmed clean. However see B32-NEW-FINDING-1/2 for separate wipe gaps.

---

## C1 Verify: Stability, no duplicates

No duplicate event types in any scenario. chat_started once per session. No C1 regression.

## Q2 Verify: chat_turn_completed_inline for inline paths

Emitted in: S1, S2, S3, S5 (x2), S6 (x2), S7. Inline paths all emit correctly. Q2 working.
