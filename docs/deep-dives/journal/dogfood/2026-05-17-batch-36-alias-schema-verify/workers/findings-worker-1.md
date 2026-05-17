# B36 Worker 1 — Dogfood Findings

**Batch**: B36 (Worker 1/7)
**HEAD**: 0c1669f
**Scenario set**: `dogfood/scenarios/chat_router_smoke.yaml` (7 scenarios)
**Port**: 8081
**Driver**: A2A JSON-RPC

---

## Executive Summary

**V/I/R/B = 0/0/7/0**

All 7 scenarios REFUTED. Key failure modes:

1. Inline routing (no workspace artifact for direct_llm) — S1
2. LLM routes to web__search for factual query instead of direct_llm — S2
3. routing_decided not emitted for skill discovery — S3
4. skill_run_completed status="finished" vs scenario spec's "success" (spec bug) — S4
5. Clarification loop instead of poem — S5
6. Multi-turn context bleed / wrong skill invoked on turn 2 — S6
7. LLM offered image generation help instead of declining — S7

---

## Alias Schema Verification (B36 Key Angle)

### D2-min verify: operation-category alias (web__search)

Request IDs: 5a09b070 / 35e803bf (from REYN_LLM_TRACE_DUMP)

web__search parameters.properties (non-empty — D2-min CONFIRMED):
  query: type=string
  max_results: type=integer

### D2-full verify: resource-category aliases

file__read parameters.properties (non-empty — D2-full CONFIRMED):
  path: type=string

file__grep parameters.properties (non-empty — D2-full CONFIRMED):
  pattern: type=string
  path: type=string
  glob: type=string
  case_sensitive: type=boolean
  max_results: type=integer

### Arg-name mismatch check

- S2 web__search call: {"query": "冪等とは"} — canonical arg, NO MISMATCH
- alias-verify web__search call: {"query": "冪等とは", "max_results": 1} — canonical args
- S4 invoke_action call: {"action_name": "skill__word_stats_demo"} — canonical

arg_mismatch_recurred: NO

---

## Per-Scenario Findings

### S1: simple_capability_question — REFUTED

Events: user_message_received (PASS), chat_turn_completed_inline (inline, not routed), no permission_denied (PASS)
Reply: Lists skills at high level — rubric PASS
Artifact: No workspace artifact (handled inline, not routed to direct_llm) — FAIL

Root cause: LLM answered inline from context. Consistent with predicted refuted=0.75.

### S2: factual_query_direct_llm — REFUTED

Events: routing_decided with action=web__search (unexpected), no permission_denied (PASS)
Reply: Explains idempotency correctly — rubric PASS
Artifact: web__search not direct_llm — FAIL

Alias schema confirmed non-empty. LLM used canonical arg name "query" (no mismatch).
Root cause: LLM treated factual query as web search candidate. Consistent with predicted refuted=0.75.

### S3: skill_discovery_request — REFUTED

Events: tool_called=list_actions, chat_turn_completed_inline — routing_decided NOT emitted (must_emit FAIL)
Reply: Full skill catalog listed — rubric PASS
Root cause: LLM used list_actions + inline response, bypassing routing path. Predicted refuted=1.0.

### S4: explicit_skill_invocation_word_stats — REFUTED (scenario spec bug)

Events: routing_decided (action=skill__word_stats_demo, PASS), skill_run_spawned (PASS)
skill_run_completed status="finished" — scenario requires status="success" (FAIL)
Artifact: word_stats_demo present — PASS
Reply: Reports stats (44 chars, 1 line) — rubric PASS

Root cause (spec bug): OS emits status="finished" for success
(skill_runner.py:543: "status": result.status or "finished").
Scenario spec uses status="success" which does not match runtime.
Recommendation: Update scenario YAML to {status: finished}.

### S5: catalog_routing_decided_emitted — REFUTED

Events: chat_turn_completed_inline (satisfies must_emit_any) — event check PASS
Reply: Asked for theme/style clarification instead of writing a poem — rubric FAIL
Root cause: LLM clarification loop. Predicted refuted=0.75.

### S6: multi_turn_pronoun_reference — REFUTED

Turn 1 reply: Correct list comprehension explanation
Turn 2: LLM invoked skill__word_stats_demo via invoke_action instead of showing code example
Reply: "skill__word_stats_demo が完了しました。入力は0文字…" — rubric FAIL

Root cause: Pronoun "それ" misresolved. LLM may have been influenced by worktree state
(prior word_stats_demo runs). Multi-turn context handling failure.

### S7: out_of_scope_graceful_decline — REFUTED

Events: chat_turn_completed_inline, no permission_denied (PASS)
Reply: "どのような画像を生成したいですか？" — asked for image details instead of declining — rubric FAIL
Root cause: gemini-2.5-flash-lite capability hallucination attractor. Model does not
auto-decline unknown capabilities. Predicted refuted=0.75.

---

## C1/Q2 Stability

A2A pattern: 8/8 POST requests 200 OK (including 1 retry for kind field fix)
No permission_denied events. No blocked scenarios.
Server stable throughout.

---

## Key Findings

1. alias schema fix D2-min/D2-full CONFIRMED: web__search, file__read, file__grep show non-empty properties
2. arg-name mismatch NOT recurred: LLM used canonical arg names
3. S4 scenario spec bug: status="finished" in runtime, "success" in scenario YAML
4. S6 multi-turn failure: pronoun reference + possible context bleed from worktree
5. S7 capability hallucination: persistent attractor for weak models on unknown capabilities
