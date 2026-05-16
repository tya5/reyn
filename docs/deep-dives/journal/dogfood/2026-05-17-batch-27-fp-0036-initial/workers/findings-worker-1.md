# Batch 27 — chat_router_smoke findings (worker 1/7)

| Item | Value |
|---|---|
| Worker | 1/7 |
| Worktree | /tmp/reyn-worktrees/b27-1 |
| Reyn agent | dogfood-b27-1 (scenarios 1-3); isolated agents for 4-7 |
| Scenario set | dogfood/scenarios/chat_router_smoke.yaml |
| HEAD | 5965b58 |
| Total scenarios | 7 |
| Verified / Inconclusive / Refuted / Blocked | 0/0/3/4 |

## Per-scenario verdict matrix

| Scenario id | Verdict | Reply judge | Events | Artifacts | 1-line evidence |
|---|---|---|---|---|---|
| simple_capability_question | refuted | PASS/PASS | skill_run_spawned ABSENT, skill_run_completed ABSENT, no permission_denied PASS | direct_llm ABSENT | Events 2026-05-17T062024.jsonl: 4 event types only; LLM 0 tool calls (trace cd153ed3) |
| factual_query_direct_llm | refuted | PASS | skill_run_spawned ABSENT, skill_run_completed ABSENT, no permission_denied PASS | direct_llm ABSENT | Events 2026-05-17T062059.jsonl: 4-event pattern; trace 211c7531 0 tool calls |
| skill_discovery_request | refuted | PASS/PASS | skill_run_spawned ABSENT, skill_run_completed ABSENT, no permission_denied PASS | direct_llm ABSENT | tool_called+tool_returned for list_actions emitted; skill_run events absent |
| explicit_skill_invocation_word_stats | blocked | N/A | N/A | N/A | BadRequestError: Duplicate function declaration found: list_actions (trace 7cba69d2) |
| catalog_routing_decided_emitted | blocked | N/A | routing_decided ABSENT | N/A | Same duplicate-list_actions crash (trace 528971cd); fresh agent s5 |
| multi_turn_pronoun_reference | blocked | N/A | user_message_received ×2 only | N/A | Both prompts received; duplicate crash on turn 1, no agent reply |
| out_of_scope_graceful_decline | blocked | N/A | N/A | N/A | Same duplicate-list_actions crash; agent s7 |

## Findings (= HIGH / MED / LOW per dogfood-discipline §A4)

### F1: skill_run_spawned / skill_run_completed never emitted for any chat turn — HIGH

- **Scenario**: simple_capability_question, factual_query_direct_llm, skill_discovery_request
- **Observation** (primary data): All three event files contain only: `chat_started`, `user_message_received`, (S3: `tool_called`, `tool_returned`), `compaction_check`, `chat_stopped`. `skill_run_spawned` and `skill_run_completed` absent in all three. Event files at:
  - `/tmp/reyn-worktrees/b27-1/.reyn/events/agents/dogfood-b27-1/chat/2026-05/2026-05-17T062024.jsonl`
  - `/tmp/reyn-worktrees/b27-1/.reyn/events/agents/dogfood-b27-1/chat/2026-05/2026-05-17T062059.jsonl`
- **Expectation** (from yaml): `must_emit: [{type: skill_run_spawned, count: ">=1"}, {type: skill_run_completed, status: success}]`
- **Gap**: Chat router responds directly via LLM (with optional tool calls) without initiating a skill run lifecycle. No `direct_llm` skill is dispatched. Direct inspection: 3/3 non-blocked scenarios show 0 skill_run events.
- **Trace file**: /tmp/reyn-worktrees/b27-1/traces/simple_capability_question.jsonl

### F2: Duplicate list_actions tool declaration crashes router — HIGH

- **Scenario**: explicit_skill_invocation_word_stats, catalog_routing_decided_emitted, multi_turn_pronoun_reference, out_of_scope_graceful_decline
- **Observation** (primary data):
  - stdout for all 4 runs: `litellm.BadRequestError: GeminiException BadRequestError - {"error": {"code": 400, "message": "Duplicate function declaration found: list_actions"}}`
  - Trace 7cba69d2 (s4): `tools (13): ['list_actions', 'describe_action', 'invoke_action', 'list_actions', 'file__read', ...]` — list_actions at positions 0 and 3.
  - Trace 528971cd (s5, fresh agent): identical duplicate pattern.
  - Pre-duplication (S1 trace cd153ed3): `tools (13): ['list_actions', 'describe_action', 'invoke_action', 'file__read', ..., 'skill__read_local_files']` — no duplicate, all unique. `skill__read_local_files` also disappears from list when duplicate appears.
  - Trigger: duplicate first appears after scenario 3's `tool_called`/`tool_returned` pair for `list_actions` enters history. Reproducible across all agents created or used after this point (verified: dogfood-b27-1-s4, s5, s6, s7, dogfood-b27-1-test, dogfood-b27-1 itself post-S3).
- **Expectation** (from yaml): valid LLM reply + skill_run events for all 4 scenarios.
- **Gap**: Router sends duplicate tool name to Gemini API, which rejects with INVALID_ARGUMENT. Router does not deduplicate tool list before LLM call. All 4 scenarios receive no reply and emit no skill events.
- **Trace file**: /tmp/reyn-worktrees/b27-1/traces/s4_fresh.jsonl (request 5751b6d8), /tmp/reyn-worktrees/b27-1/traces/s5_fresh.jsonl (request 528971cd)

### F3: routing_decided event never emitted — HIGH

- **Scenario**: catalog_routing_decided_emitted (blocked); also absent in S1-S3 (non-blocked)
- **Observation** (primary data): Direct inspection of all 3 non-blocked scenario event files — `routing_decided` type absent in all. S5 event file at `/tmp/reyn-worktrees/b27-1/.reyn/events/agents/dogfood-b27-1-s5/chat/2026-05/2026-05-17T062253.jsonl` shows only `chat_started`, `user_message_received`, `chat_stopped`.
- **Expectation** (from yaml S5): `must_emit: [{type: routing_decided, count: ">=1"}]`
- **Gap**: `routing_decided` event (FP-0034 Phase 6) not emitted in any chat turn. Direct verification: 3/3 non-blocked scenarios inspected, 0 `routing_decided` emissions observed.
- **Trace file**: /tmp/reyn-worktrees/b27-1/traces/catalog_routing_decided_emitted.jsonl

### F4: direct_llm artifacts not created for any scenario — MED

- **Scenario**: simple_capability_question, factual_query_direct_llm, skill_discovery_request
- **Observation** (primary data): `find /tmp/reyn-worktrees/b27-1/.reyn -name "artifacts" -type d` returns empty. No artifact dirs under any agent path.
- **Expectation** (from yaml): `artifacts: [{skill: direct_llm, present: true}]`
- **Gap**: Downstream of F1 — no skill run initiated means no skill workspace/artifacts created. Dependent finding.
- **Trace file**: N/A

## Calibration check

All 7 scenarios diverged >=30 pp from their outcome_prediction bands:

| Scenario | Predicted verified | Predicted blocked | Actual verdict | Notes |
|---|---|---|---|---|
| simple_capability_question | 0.70 | 0.05 | refuted | Reply correct but event contract broken |
| factual_query_direct_llm | 0.75 | 0.02 | refuted | Same — reply correct, events absent |
| skill_discovery_request | 0.65 | 0.05 | refuted | tool_called emitted but not skill_run events |
| explicit_skill_invocation_word_stats | 0.60 | 0.05 | blocked | Duplicate tool bug not anticipated |
| catalog_routing_decided_emitted | 0.65 | 0.05 | blocked | Duplicate tool bug |
| multi_turn_pronoun_reference | 0.60 | 0.05 | blocked | Duplicate tool bug |
| out_of_scope_graceful_decline | 0.65 | 0.03 | blocked | Duplicate tool bug |

Root causes of divergence:
1. Outcome predictions assumed the chat router dispatches all queries through a `direct_llm` skill run. Actual behavior: router LLM answers directly with no skill lifecycle.
2. The duplicate `list_actions` bug was not in the prediction model — it blocks 4/7 scenarios entirely.

## Blockers

**F2 (Duplicate list_actions) blocks 4/7 scenarios**: The bug is deterministic post-scenario-3. `reyn chat` with ANY agent after a `list_actions` tool call exists in history fails with `GeminiException BadRequestError: Duplicate function declaration found: list_actions`. Primary data: stdout error text + trace files showing `list_actions` at positions 0 and 3 in the 13-item tools array. Scenarios 4, 5, 6, 7 could not be observed to completion.
