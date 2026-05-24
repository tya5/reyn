# Dogfood B54 — W6 scenario set 3→5 redesign baseline + carry-over re-measurement

**Date**: 2026-05-24  **HEAD**: `e52fff99` (= post-PR #863/#864/#865/#866/#867 merges + sandbox_2 wave: F3 fix #645/#648, F6 fix #635, replay infra #861, doc cleanup #863, **W6 scenario redesign #867**)

## Headline

| Metric | B53 | B54 | Δ |
|---|---|---|---|
| V | 29/48 = 60.4% | **36/50 = 72.0%** | **+7V / +11.6pp** |
| I | 5 | 1 | -4 |
| R | 14 | 13 | -1 |
| B | 0 | 0 | 0 |

**Primary axis (PR #867 W6 scenario set 3→5 redesign)**: **confirmed delivered**.
- S2 redesigned (`plan_explain_with_code_references` → `plan_deep_dive_permission`): **R→V** ✓
- S4 new (`plan_onboarding_skill_mcp`): baseline **V** ✓
- S5 new (`plan_build_multi_doc_agent`): baseline **V** ✓
- W6 V rate: 1/3 (33%) → 4/5 (80%) = +47pp

**Carry-over verifications**:
- W7-s3 LiteLLM/Gemini `BadRequestError` (B53 N=1): **did NOT recur** → dismissed as noise per `feedback_cross_batch_pattern_threshold` (N=1→N=0)
- W2 sub-agent stuck-loop (B53 single observation): **did NOT recur** → dismissed as noise

**New findings (B54 surfacing)**:
- **W2-S5 eval postprocessor schema bug (HIGH)**: `compute_eval_score` returns `None` instead of string per eval postprocessor; all 5 eval runs failed
- **W2-S6 long-session plan-mode override (MEDIUM)**: plan mode triggered instead of skill dispatch; `skill_run_spawned=0`, routing answer not produced

## Per-worker delta

| Worker | Scenario set | B53 V | B54 V | Δ | Headline |
|---|---|---|---|---|---|
| W1 | chat_router_smoke | 5/7 | 5/7 | 0 | Stable. S5 + S7 persistent R (clarification-bias / out-of-scope hallucination). |
| W2 | stdlib_skills_core | 2/7 | **5/7** | **+3** | Major recovery. S1-S4/S7 V. Two new findings: eval postprocessor schema bug (S5, HIGH) + long-session plan-mode override (S6, MEDIUM). |
| W3 | control_ir_ops | 6/9 | 6/9 | 0 | V flat. Internal shifts: S7 R→I (env: `operation__recall` vs `rag.operation__recall` namespace mismatch). S6 lint clarification-asking + S9 ask_user inline-bypass remain R. |
| W4 | permissions_and_safety | 7/8 | **8/8** | **+1** | Clean sweep. S2 mcp_install I→V (skill_run_spawned + outcome reported, was previously silently failing). |
| W5 | multi_agent_and_mcp | 2/7 | 2/7 | 0 | V flat but I→R shift (-1 I, +1 R). Structural observation: `routing_decided` and `skill_run_spawned` not emitted when LLM handles capability queries inline. |
| W6 | plan_mode (5 scenarios) | 1/3 | **4/5** | **+3** | **PRIMARY axis confirmed**: S2 redesign R→V, S4/S5 new V. S3 `plan_summary_across_n_files` remains R (= wrong-path attractor: bare filenames stripped of `docs/concepts/` prefix; separate from S2 redesign). |
| W7 | long_session_v1 | 6/7 | 6/7 | 0 | V flat. B53 S3 BadRequestError dismissed (N=0 in B54). S5 new R: `general_python_chain` dispatched to sub-agent instead of inline code generation. |

## PR #867 W6 redesign verification — primary axis

**Redesign recap** (from B53 carry-over deep-dive 2026-05-24):
- B53 retrospective identified W6-S2 `plan_explain_with_code_references` as 8+ batch persistent R/R.
- Deep-dive via `scripts/dogfood_trace.py --mode llm-context` observation identified **scenario design flaw** (= dimension 4 audit per `feedback_scenario_design_audit_checklist`): old prompt asked to explain plan tool from 2 related files (= single-concept unified explanation, NOT multi-source synthesis). Rubric forced `plan_emitted` but prompt was structurally plan-unnatural.
- User pushback rejected SP/tool-description overfit ("plan tool 最適化して他がダメになるような修正は禁止")
- True fix: scenario redesign. PR #867 landed:
  - S2 redesigned + renamed → `plan_deep_dive_permission` (3 facet × 3 doc, plan-natural)
  - S4 added → `plan_onboarding_skill_mcp`
  - S5 added → `plan_build_multi_doc_agent`

**B54 verification**:
- S2 `plan_deep_dive_permission`: **V** — all 3 docs read (permission-model, manage-permissions, permissions config reference), 3 facets organized (kinds / setup / pitfalls), config keys cited.
- S4 `plan_onboarding_skill_mcp`: **V** — all 3 docs read (phase-vs-skill-vs-os, mcp, your-first-skill), 3 questions answered with evidence.
- S5 `plan_build_multi_doc_agent`: **V** — all 3 docs read (build-an-agent-team, manage-permissions, mcp), 3 facets organized.

Plan-natural prompt design delivered: 4/5 V vs B53's 1/3 V, +47pp on W6.

S3 (`plan_summary_across_n_files`) remains R — but the failure mode is different from S2's. S3's R is from **wrong-path attractor** (= LLM strips `docs/concepts/` prefix from filenames, falls back to CLAUDE.md). This is a separate carry-over not addressed by PR #867's scope, persists as W6 family attractor.

## V-rate decomposition (per `feedback_v_metric_decomposition`)

vs B53 (= 29/48 = 60.4%):

| Component | Effect | Detail |
|---|---|---|
| OS wins / dilution | 0 | No new OS scenarios. |
| Surface migration | 0 | No new surface migration since B53. |
| Worktree freshness | 0 | All workers fresh-cleaned per dispatch script. |
| Rubric coverage | +2 | W6 expanded 3→5 scenarios. Both new scenarios (S4/S5) baseline V. |
| Infra residual | 0 | Dispatch script env-injections held. No new infra debt. |
| LLM noise | ±2 | Within band. W2 +3 V partly noise (S1-S4 recoveries), W5 1 R→I → R shift. |
| New / re-measured findings | +1 | W6-S2 redesign confirmed (R→V structural fix). |
| Component improvements | +2 to +3 | W4-S2 I→V (mcp_install state-change fix), W2-S1/S2/S3/S4 stabilisations. |
| Upstream env flake | 0 | W7-s3 BadRequestError dismissed. |
| **Net** | **+7V** (matches headline) | Decomposition: +2 from W6 denominator expansion + +1 from W6-S2 structural fix + +2-3 from W2/W4 stabilisations + ±2 LLM noise band ≈ +7V observed |

## Carry-over re-measurement decisions

| Carry-over | B53 N | B54 N | Decision |
|---|---|---|---|
| W7-s3 LiteLLM `BadRequestError` flake | 1 | 0 | **Dismiss as noise** (= cross-batch threshold N≥3 not reached, single-observation now disappeared) |
| W2 sub-agent stuck-loop | 1 | 0 | **Dismiss as noise** (= did not recur in B54 W2) |
| W6 plan-mode reply-path family (S3 `plan_summary_across_n_files`) | 4+ | 5+ | **Continue tracking** — same R shape in B54 (wrong-path attractor). S2 redesign separates this from old S2/S3 entanglement; S3 is now isolated family member. Candidate for B55+ targeted investigation. |
| W1-S5 catalog_routing_decided clarification-bias | 8+ | 9+ | **Continue tracking** — W1 ceiling, weak-tier creative-ambiguity attractor. |
| W1-S7 out_of_scope_graceful_decline | 8+ | 9+ | **Continue tracking** — W1 ceiling, weak-tier false-capability-claim attractor. |

## New B54 findings — actionable

### NF-W2-S5 — eval postprocessor schema bug [HIGH]

**Scenario**: stdlib_skills_core / S5 `eval_run_direct_llm`
**Symptom**: 5 eval runs all failed during postprocessor stage
**Diagnosis**: `compute_eval_score` (per worker output) returns `None` instead of expected string-typed value, breaking eval postprocessor schema
**Verdict**: R (rubric requires eval completion)
**Action candidate**: trace-patch-replay on one failing eval run + inspect `eval/postprocessor/compute_eval_score` shape; if schema mismatch confirmed, narrow fix to type contract.

### NF-W2-S6 — long-session plan-mode override [MEDIUM]

**Scenario**: stdlib_skills_core / S6 `chat_compactor_long_session`
**Symptom**: plan mode triggered instead of skill dispatch; `skill_run_spawned=0`, final reply did not answer routing question
**Diagnosis**: plan tool preferred over direct invoke_action for long-session compactor routing query
**Verdict**: R
**Action candidate**: trace-patch-replay to observe LLM's plan vs invoke decision; check if SP/tool description bias toward plan in this context (= possibly post-PR #867 description strengthening had cross-scenario impact?)

### NF-W3-S6 — lint clarification asking [LOW, persistent]

**Scenario**: control_ir_ops / S6 `lint_a_skill`
**Symptom**: LLM asked for clarification instead of running lint op; `routing_decided` + `lint_completed` not emitted
**Verdict**: R (carry-over from earlier batches likely; not new but isolated this batch)
**Action candidate**: tracking only; weak-tier clarification-bias family

### NF-W3-S7 — recall op namespace mismatch [LOW, env]

**Scenario**: control_ir_ops / S7 `recall_indexed_source`
**Symptom**: `tool_failed` — LLM called `operation__recall` vs registered `rag.operation__recall`
**Verdict**: I (env classification — namespace mismatch)
**Action candidate**: doc/SP add namespace examples for rag.operation surface; minor.

### NF-W5 — routing_decided / skill_run_spawned non-emission [STRUCTURAL, observation]

**Scenario set**: multi_agent_and_mcp (W5)
**Symptom**: 5/7 R, multiple scenarios with rubric requiring `routing_decided` or `skill_run_spawned` not emitted; LLM handles inline without skill dispatch
**Diagnosis**: structural — rubric requirement vs actual chat-router inline-reply path. Either rubric over-strict OR event emission gap.
**Action candidate**: scenario set audit per dimension 4 (rational alternative paths) — if inline-reply is valid behavior, rubric should accept `chat_turn_completed_inline` as well as `routing_decided`.

### NF-W7-S5 — sub-agent dispatch attractor [LOW, observation]

**Scenario**: long_session_v1 / S5 `general_python_chain`
**Symptom**: LLM dispatched to sub-agent instead of inline code generation; reply narrated "I have dispatched your request" with no code
**Verdict**: R (rubric requires asyncio.Queue code in final reply)
**Action candidate**: tracking only; possibly related to W6 plan-mode bias post #867? Cross-check trace.

## Predicted vs actual — calibration

**Prediction (from batch_b54.yaml)**:
- W6 redesigned S2 + new S4/S5: optimistic +2V, conservative +1V, pessimistic 0V
- Total V: 29-33/50 = 58-66% range
- Other workers: ±1-2 V LLM noise band

**Actual**: 36/50 = 72%, **+7V**

**Calibration assessment**:
- W6 axis: +3V (= 1 from S2 redesign + 2 from new scenarios baseline V) — **better than optimistic** (predicted +2V, actual +3V)
- Other workers: W2 +3 (within noise band but at upper edge), W4 +1 (= component fix, anticipated), W3 0 (flat), W7 0 (flat, BadRequestError dismissed as predicted), W5 0 (flat)
- **Net +7V vs predicted +1 to +5V range** — exceeded optimistic by +2V
- Excess attributable to: W2 stabilisations (= mcp_install state-change fixes had broader impact than tracked, W4 also benefitted), W6 component improvements

**Brier-style accuracy**: B54 prediction vs actual = within optimistic envelope but underestimated cross-worker stabilisation effects.

## Implications for B55+

1. **W6 plan-mode reply-path family** (= S3 `plan_summary_across_n_files` isolated R): now disentangled from S2/S5 redesign. Candidate for B55 targeted deep-dive — possibly via similar scenario design audit (= is the prompt also dim-4 NG?), or via SP-level fix if the wrong-path attractor (= `docs/concepts/` prefix stripping) is reproducible.

2. **NF-W2-S5 eval postprocessor schema bug** [HIGH]: not landing-blocking but eval workflow is broken. trace-patch-replay first to confirm structural before fix dispatch.

3. **NF-W2-S6 long-session plan-mode override** [MEDIUM]: investigate possible PR #867 cross-scenario impact. If plan description strengthening caused unrelated scenarios to over-prefer plan, may need scope adjustment. (Watch: NF-W7-S5 sub-agent dispatch attractor may be same family.)

4. **NF-W5 routing_decided non-emission** [STRUCTURAL]: scenario set audit candidate. If rubrics are over-strict for chat-router-inline-reply pattern, loosen `must_emit_any` to include `chat_turn_completed_inline`.

5. **W1 S5/S7 persistent attractors**: weak-tier ceilings. Continue tracking, no action unless N triples-or-more.

## Discipline applied

- ✅ User param fixed vs B53 ([[feedback_user_params_fixed_in_comparison]]: hot_list_n=10, flash-lite weak only, default seed, fresh mode)
- ✅ Past-comparison table in batch report ([[feedback_batch_report_past_comparison]])
- ✅ Pre-conclusion observation checklist applied for "BadRequestError dismissed" decision (= N=1 single-obs threshold check) ([[feedback_pre_conclusion_observation_checklist]])
- ✅ Cross-batch pattern threshold for carry-over decisions ([[feedback_cross_batch_pattern_threshold]])
- ✅ V umbrella decomposition with 7-component frame ([[feedback_v_metric_decomposition]])
- ✅ Sub-agent scope bounding (= 7 workers each 1 JSON deliverable, hard cap 50 tool uses + 15 min) ([[feedback_subagent_scope_bounding]])
- ✅ No strong model for Reyn internal ([[feedback_no_strong_model]]); user-approved sonnet for sandbox sub-agent dispatch
- ✅ Per-PR fire-wire broker notify
- ✅ PR Tier rule self-check ([[feedback_pr_review_tier_rule_check]]): yaml-only PR, N/A noted

## Files

- `dogfood/batch_b54.yaml` — batch config (50 scenarios, post-PR #867 W6 expansion)
- `docs/deep-dives/journal/dogfood/2026-05-24-batch-54-w6-redesign-baseline/workers/results-worker-{1..7}.json` — per-worker raw output
- `docs/deep-dives/journal/dogfood/2026-05-24-batch-54-w6-redesign-baseline/aggregate.json` — combined aggregate
- This retrospective
