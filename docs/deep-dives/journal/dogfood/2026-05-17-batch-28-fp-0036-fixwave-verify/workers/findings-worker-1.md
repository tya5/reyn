# Batch 28 — chat_router_smoke findings (worker 1/7)

| Item | Value |
|---|---|
| Worker | 1/7 |
| HEAD | f5a6866 |
| B27 baseline | V/I/R/B = 0/0/3/4 |
| B28 actual | V/I/R/B = 0/0/7/0 |
| Δ verified | 0 |
| C1 regression | none — B27-C1 fix confirmed working (14 unique tools across all 7 scenarios, no duplicates) |
| routing_decided emit | 3/7 scenarios emitted routing_decided (S2 invoke_action/web__search, S4 invoke_action/word_stats_demo, S6 invoke_action/web__search) |

## Per-scenario verdict matrix

| Scenario | B27 verdict | B28 verdict | Reply | Events | Artifacts | C1 check | routing_decided | Evidence |
|---|---|---|---|---|---|---|---|---|
| simple_capability_question | blocked | refuted | PASS | PASS | FAIL (no direct_llm artifact) | PASS (14 unique) | N/A (not required) | LLM replied inline, no skill_run spawned |
| factual_query_direct_llm | blocked | refuted | PASS | PASS | FAIL (no direct_llm artifact) | PASS (14 unique) | emitted (not required by scenario) | invoke_action(web__search); routing_decided source=invoke_action |
| skill_discovery_request | blocked | refuted | PASS | FAIL (routing_decided absent) | FAIL (no direct_llm artifact) | PASS (14 unique) | NOT emitted (required) | LLM called list_actions directly — routing_decided only fires for __ or invoke_action calls |
| explicit_skill_invocation_word_stats | blocked | refuted | FAIL (skill error) | PARTIAL (spawned+routing_decided fired, skill_run_completed absent) | FAIL (no word_stats_demo output artifact) | PASS (14 unique) | emitted (source=invoke_action) | python.safe permission denied: reyn.yaml has python.pure:allow but key is python.safe |
| catalog_routing_decided_emitted | refuted | refuted | FAIL (asked clarification instead of poem) | FAIL (routing_decided absent) | FAIL (no direct_llm artifact) | PASS (14 unique) | NOT emitted (required) | LLM replied inline with no tool call |
| multi_turn_pronoun_reference | refuted | refuted | PASS (2nd reply has Python code example) | PASS | FAIL (no direct_llm artifact) | PASS (14 unique; msgs=4 in turn 2) | emitted (not required; web_search in turn 2) | Multi-turn worked; pronoun reference resolved correctly |
| out_of_scope_graceful_decline | refuted | refuted | FAIL (asked clarification instead of declining) | PASS | FAIL (no direct_llm artifact) | PASS (14 unique) | N/A (not required) | LLM asked clarification instead of declining |

## Findings

### B28-F1: B27-C1 fix confirmed — no duplicate tools (RESOLVED)

All 7 scenarios show exactly 14 unique tools in LLM payloads. No duplicates of list_actions, describe_action, invoke_action, or search_actions detected. Primary evidence: dogfood_trace.py --mode llm-payloads for all 7 traces, each showing tools=(14) with no repetition.

### B28-F2: B27 blocked scenarios now unblocked (0 blocked vs 4 in B27)

B27-C1 fix eliminated the Gemini INVALID_ARGUMENT duplicate declaration error. All 4 previously-blocked scenarios (S1, S2, S3, S4) now complete. However, they fail on other dimensions (artifact, routing_decided, reply).

### B28-F3: direct_llm workspace artifact never created — B27-Q1 persists

Scope: S1, S2, S3, S5, S6, S7. Primary evidence: .reyn/artifacts/ contains only word_stats_demo/_input/v01_unknown.json after all 7 scenario runs. No direct_llm artifact directory. Router dispatches conversational turns inline (direct LLM response) without spawning a direct_llm skill run.

### B28-F4: routing_decided not emitted for list_actions calls (NEW observable finding)

S3 called list_actions(category=["skill"]) directly. No routing_decided event fired. Source: router_loop.py lines 853-886 — routing_decided only fires when _rd_name == "invoke_action" OR "__" in _rd_name. list_actions, describe_action, search_actions are not covered. S3 requires routing_decided and fails.

### B28-F5: python.pure vs python.safe permission key mismatch — NEW, blocks word_stats_demo (S4)

Primary evidence: CLI error "safe python step ./stats.py:compute_text_stats denied by user". reyn.yaml line 29 has "python.pure: allow" but permissions.py line 873 uses key "python.safe/{module}:{function}". _is_config_approved("python.safe") finds no match since config has "python.pure". Non-interactive mode + no unsafe_python flag -> approve returns False. This was hidden in B27 because S4 was blocked by C1 before reaching the python step.

### B28-F6: S5 / S7 LLM asks clarification instead of acting / declining (PERSISTS)

S5 "短い詩を書いてください" -> agent asked for theme (did not write poem). S7 "画像を生成してください" -> agent asked what kind of image (did not decline). Both were refuted in B27 for different reasons. LLM behavioral tendency to request clarification for ambiguous/capability-gap inputs.

## Comparison to B27 worker 1

**Resolved**: B27-C1 (duplicate tools) — fixed, 0 blocked. B27-H1 (plan tool) — plan visible in all payloads. B27-H2 (web tools) — web__fetch/web__search in tool list. B27-M2 (file__grep) — absent. B27-M5 (file__list/reyn.source__list) — present.

**Persisting**: B27-Q1/B28-F3 (direct_llm artifact gap). B27-M1/B28-F4 (routing_decided for list_actions). S5/S7 behavioral failures (B28-F6).

**New**: B28-F5 (python.pure vs python.safe mismatch) — previously hidden by C1 block.
