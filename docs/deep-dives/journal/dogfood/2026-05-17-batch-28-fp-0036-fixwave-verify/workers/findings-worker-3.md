# B28 Worker 3 Findings — control_ir_ops
Generated: 2026-05-17T07:55:00Z
HEAD: f5a6866
Scenario set: control_ir_ops.yaml (9 scenarios)
B27 baseline V/I/R/B: 0/3/3/3

## Summary

Final: V=6 I=1 R=2 B=0  (delta_verified=+6 vs B27 baseline of 0)

### B28 verification angles

| Angle | Result |
|-------|--------|
| C1 (no dup func decl) | PASS — all 9 scenarios: no duplicates found in any request |
| routing_decided emit | PASS — all 9 scenarios emitted routing_decided >=1 |
| M2 (no file__grep) | PASS — 0 scenarios called file__grep |

---

## S1: file_read_via_chat — VERIFIED

- LLM calls: 2, elapsed: 4.9s
- Tools: invoke_action
- Events: routing_decided(1), tool_executed(1) — all must_emit satisfied
- C1: True, M2: True
- Reply: Correctly summarised principles.md, mentioned P1-P8 concepts
- Notes: routing_decided emitted (Q1 fix verified for inline ops).

## S2: file_glob_grep — VERIFIED

- LLM calls: 2, elapsed: 4.5s
- Tools: invoke_action
- Events: routing_decided(1), tool_executed(1) — all must_emit satisfied
- C1: True, M2: True (file__glob used, NOT file__grep)
- Reply: No matching files found (factually correct — none exist)
- Notes: M2 confirmed — file__grep never called.

## S3: web_search_query — VERIFIED

- LLM calls: 2, elapsed: 7.3s
- Tools: web__search (direct hot alias)
- Events: routing_decided(1), web_search_started(1), web_search_completed(1) — all satisfied
- C1: True, M2: True
- Notes: All web search P6 events fired correctly.

## S4: web_fetch_url — VERIFIED

- LLM calls: 2, elapsed: 6.7s
- Tools: invoke_action
- Events: routing_decided(1), web_fetch_started(1), web_fetch_completed(1) — all satisfied
- C1: True, M2: True
- Reply: Summarised Python 3.12 features (PEP 695 mentioned)

## S5: sandboxed_exec_simple — INCONCLUSIVE (environment gate)

- LLM calls: 2, elapsed: 4.4s
- Tools: invoke_action (tried exec__run, got "no routing rule for category 'exec'")
- Events: routing_decided(1) — sandboxed_exec_started/completed NOT emitted
- C1: True, M2: True
- Root cause: No sandbox backend configured (sandbox.backend not set). is_exec_available() returns False -> exec category hidden from catalog. Expected events cannot fire without sandbox backend. Gating logic works correctly.
- Classification: Inconclusive (environment constraint, not a regression).

## S6: lint_a_skill — REFUTED

- LLM calls: 3, elapsed: 5.0s
- Tools: invoke_action (invoked skill__skill_improver)
- Events: routing_decided(1), skill_run_spawned(1), skill_run_failed(1) — lint_completed NOT emitted
- C1: True, M2: True
- Root cause: LLM routed to skill__skill_improver (semantic closest for "lint a skill"), which failed due to --allow-unsafe-python requirement. lint_completed event is only emitted from inside the skill execution, not reachable without the flag.
- Classification: Refuted — lint_completed never fired. Behavioral finding: "lint a skill" routes to skill_improver, fails on unsafe-python gate.

## S7: recall_indexed_source — VERIFIED

- LLM calls: 2, elapsed: 4.0s
- Tools: invoke_action
- Events: routing_decided(1) — minimal must_emit satisfied
- C1: True, M2: True
- Reply: "Unable to find documentation regarding phase rollback" — graceful not-found, no fabrication.
- Notes: Graceful not-found path satisfies the rubric.

## S8: judge_output_direct — VERIFIED

- LLM calls: 14, elapsed: 15.5s
- Tools: invoke_action
- Events: routing_decided(1), skill_run_spawned(1), tool_executed(1) — all must_emit satisfied
- C1: True, M2: True
- Notes: judge_phase invoked as sub-skill. skill_run_failed also emitted (postprocessor python gate) but all must_emit checks pass. Reply is raw phase output JSON.

## S9: ask_user_round_trip — REFUTED

- LLM calls: 12, elapsed: 20.1s
- Tools: describe_action, invoke_action
- Events: routing_decided(1), skill_run_spawned(1), skill_run_completed(1) — user_intervention_requested/received NOT emitted
- C1: True, M2: True
- Root cause: skill_builder asked the skill name via chat reply (not via formal ask_user control IR op). Second stdin line "my_demo_skill" received as user_message_received. Formal ask_user op path never taken -> no user_intervention_requested/received events.
- Classification: Refuted — formal ask_user op path not exercised.

---

## B28 Verification Angles — Detail

### C1 (hot-list filter / no dup func decl)
Inspected all 9 trace files. No duplicate function names in tools array in any request. C1 fix working.

### routing_decided (Q1 fix)
routing_decided count >=1 in all 9 scenarios (direct observation from events). Q1 fix confirmed for S1-S7 inline ops and S8/S9 skill scenarios.

### M2 (file__grep dropped from seed)
S2 used invoke_action with file__glob. file__grep never called. M2 fix confirmed.

---

## Open Issues

1. S5: Needs sandbox.backend=auto in reyn.yaml to be testable. exec category currently always hidden.
2. S6: "Lint a skill" maps to skill_improver which needs --allow-unsafe-python. Scenario needs redesign or flag added.
3. S9: skill_builder resolves name via chat reply, not formal ask_user op. Scenario may need to target a skill that explicitly calls ask_user in control_ir.
