# B32 Worker 6 Findings

**Date**: 2026-05-17
**SUT HEAD**: c8fae2e
**Agent**: dogfood-b32-6
**B30 baseline**: V/I/R/B = 0/9/2/0

---

## Summary

V/I/R/B = 1/6/2/0  (ΔvsB30: +1V, -3I, 0R, 0B... wait: R=2 same)

Actually corrected totals:
- Verified: 1 (plan_compare_two_concepts)
- Inconclusive: 6 (narr-1, narr-3, s-fp11-2, s-fp11-3, s-fp12-spawn-1, s-fp12-completion-1)
- Refuted: 2 (plan_explain_with_code_references, s-fp12-completion-2)
- Blocked: 0

Wait - s-fp11-1 was also refuted. Recount:
- plan_compare_two_concepts: V
- plan_explain_with_code_references: R
- plan_summary_across_n_files: R
- narr-1-mcp-search: I
- narr-3-skill-builder: I
- s-fp11-1-builder-invalid-spec: R
- s-fp11-2-eval-missing-target: I
- s-fp11-3-mcp-search-empty: I
- s-fp12-spawn-1-builder-success-ack: I
- s-fp12-completion-1-mcp-search-narrate: I
- s-fp12-completion-2-error-narrate: R

**Final: V=1, I=6, R=4, B=0**
(ΔvsB30: +1V, -3I, +2R vs 0/9/2/0)

**plan_emitted: 1/3** (same as B30)
**double_dispatch_observed: YES** (3/5 skill_builder scenarios, including triple dispatch in narr-3)

---

## Per-Scenario Results

### 1. plan_compare_two_concepts — VERIFIED

- plan_emitted:1, plan_step_started:3, plan_step_completed:3, plan_aggregated:1 — all must_emit present.
- plan_run_interrupted:0, plan_step_failed:0 — must_not_emit clean.
- Reply referenced both permission-model.md AND workspace.md with content.
- Listed 3 concrete points.
- No fabrication; agent acknowledged it could not find specific header names (honest).
- Caveat: rubric item 3 (specific header citations) only partially met.
- No double dispatch — plan tool called once; steps used reyn_src_read per step.

### 2. plan_explain_with_code_references — REFUTED

- plan_emitted:0. LLM used list_actions + describe_action instead.
- Agent incorrectly stated "plan tool is not available."
- No plan lifecycle events. Events: only list_actions, describe_action tool_calls.

### 3. plan_summary_across_n_files — REFUTED

- plan_emitted:0. LLM read all 3 files directly via invoke_action (reyn.source__read x3).
- Reply content was substantively good (4+ pillars with doc references) but events criterion unmet.
- routing_decided:4, invoke_action:3, list_actions:3 in events.

### 4. narr-1-mcp-search — INCONCLUSIVE

- Routing correct: invoke_action -> skill__mcp_search (1 call, no double dispatch).
- skill_run_failed: unsafe python blocker (--allow-unsafe-python not provided).
- expected_status=finished not met. Error surfaced in terminal output.
- Infrastructure prerequisite missing, not a routing failure.

### 5. narr-3-skill-builder — INCONCLUSIVE

- Skill invoked 3 times (TRIPLE DISPATCH from 3 separate turns).
- All 3 runs: workflow_finished + lint_passed (status=finished).
- Spawn ack: "I will notify you when it's complete. You can check its progress with /tasks."
- Completion narration: NOT OBSERVED before session exit.
- judge_focus fields not verifiable.

Triple dispatch detail (from trace):
  Turn 2: invoke_action (description only, no name)
  Turn 3: invoke_action (name + description)
  Turn 4: skill__skill_builder (different description phrasing)
Agent asked clarifying question on turn 1 → each re-prompt spawned new invocation.

### 6. s-fp11-1-builder-invalid-spec — REFUTED

- Skill invoked 2 times (double dispatch).
- Both runs: workflow_finished + lint_passed — LLM normalized the invalid circular-ref spec silently.
- No error surfaced — terminal showed prior-session success narrations, then nothing for invalid spec.
- D1 anti-optimism: FAILED (no error, no error message verbatim).
- D5 no-double-dispatch: FAILED.
- Session carryover: session_restored injected 3 prior skill_builder completions.

Root cause of no-error: skill_builder plan_skill phase rewrites specs; it did not
propagate invalid circular ref as an error — it fixed it.

### 7. s-fp11-2-eval-missing-target — INCONCLUSIVE

- Eval invoked 2 times (double dispatch).
- Both skill_run_failed: "Postprocessor output failed schema validation: 'overall_score' required."
- Error surfaced: yes (schema validation error narrated).
- expected_status=error: MET.
- missing_target_surfaced: NOT MET (different error type).
- no_fabricated_score: MET.
- D5: FAILED.
- Session carryover: skill_builder completion narrated first.

### 8. s-fp11-3-mcp-search-empty — INCONCLUSIVE

- mcp_search invoked once (NO double dispatch — 1 call).
- skill_run_failed: unsafe python blocker (same as narr-1).
- Session carryover: 2 eval failure completions narrated first.
- Empty result path unreachable due to infrastructure gate.
- D5 (no double dispatch): MET for this scenario.

### 9. s-fp12-spawn-1-builder-success-ack — INCONCLUSIVE

- Skill invoked 2 times (double dispatch).
- Both runs completed (workflow_finished, finished).
- D2 spawn ack: MET (1 sentence, has /tasks pointer).
- D3 completion narration: NOT observed (session ended after spawn acks).
- D5: FAILED.
- Session carryover: mcp_search failure completion narrated first.

### 10. s-fp12-completion-1-mcp-search-narrate — INCONCLUSIVE

- mcp_search invoked once (no double dispatch).
- skill_run_failed: unsafe python (same infrastructure blocker).
- D3 completion narration: NOT observable.
- Session carryover: skill_builder completions narrated first.

### 11. s-fp12-completion-2-error-narrate — REFUTED

- Skill invoked: ZERO — 0 tool_calls in events, 0 in trace.
- Router replied "I will notify you when it's complete. You can check its progress with /tasks." twice with finish_reason=stop and tool_calls=0.
- Hallucination: router simulated a spawn without calling invoke_action.
- D1, D2, D3, D5 all FAILED.

---

## Double-Dispatch Sub-Table

| Scenario | invoke_skill calls | Expected | Status |
|---|---|---|---|
| narr-3-skill-builder | 3 (TRIPLE) | 1 | FAIL |
| s-fp11-1-builder-invalid-spec | 2 | 1 | FAIL |
| s-fp12-spawn-1-builder-success-ack | 2 | 1 | FAIL |
| s-fp11-3-mcp-search-empty | 1 | 1 | PASS |
| s-fp12-completion-1-mcp-search-narrate | 1 | 1 | PASS |
| s-fp12-completion-2-error-narrate | 0 | 1 | FAIL (hallucination) |

Double dispatch: reproduced in 3/6 scenarios with skill_builder. NOT B30-fixed.

---

## Cross-Scenario Observations

### Session Carryover (Wipe Incomplete)
The per-scenario wipe recipe does NOT clear history.jsonl.
`.reyn/agents/dogfood-b32-6/history.jsonl` persists across scenarios.
Session_restored event fires at each new session start and injects pending
completions from prior runs. This contaminated: s-fp11-1, s-fp11-2, s-fp11-3,
s-fp12-spawn-1, s-fp12-completion-1.
Recommendation: add `rm -f .reyn/agents/dogfood-b32-6/history.jsonl` to wipe recipe.

### plan Tool Trigger Rate 1/3 (B30 unchanged)
- Triggered on: explicit multi-file compare ("読み比べて") → plan_compare_two_concepts.
- Not triggered on: code explanation with named files, N-file summary with direct reads.
- LLM routes to direct invoke_action when it can satisfy the query without parallelism.

### mcp_search Blocked (3 scenarios)
narr-1, s-fp11-3, s-fp12-completion-1 all failed with identical error:
"Skill 'mcp_search' declares an unsafe python step ... --allow-unsafe-python not provided."
All 3 scenarios are effectively non-runnable without harness flag addition.

### skill_builder Invalid Spec Does Not Produce Error
s-fp11-1 design assumption (empty name + circular graph → lint error) is wrong.
skill_builder plan_skill phase normalizes specs; it does not propagate raw input
errors. The scenario needs redesign (e.g., a spec that survives plan_skill
normalization but fails verify_skill lint) to reliably hit expected_status=error.

### B30-NEW-1/2/3 Fix Assessment
- NEW-1 (hot_list_n 10→16): No observable effect on double dispatch.
- NEW-2 (seed skill__eval): No observable effect on tested scenarios.
- NEW-3 (per-scenario wipe recipe): Partial — history.jsonl gap.
