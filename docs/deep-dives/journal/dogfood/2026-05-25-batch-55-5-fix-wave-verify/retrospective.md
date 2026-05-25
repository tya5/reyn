# Dogfood B55 — 5-fix wave verify (post R-2 / R-3 / W6-S3 / R-5a / R-6 landing)

**Date**: 2026-05-25  **HEAD**: `97d762a4` (= post PR #875 + #880 + #883 + #912 + #914)

## Headline — major prediction miss

| Metric | B54 | **B55** | Δ | **Predicted** | **Miss** |
|---|---|---|---|---|---|
| V | 36/50 = 72.0% | **34/50 = 68.0%** | **-2V / -4pp** | +6-8V / 84-88% | **~-8 to -10V** |
| I | 1 | 3 | +2 | — | — |
| R | 13 | 13 | 0 | — | — |
| B | 0 | 0 | 0 | — | — |

The pre-batch prediction (= cumulative effect of 5 fixes) overshot by **~8-10V**. Actual outcome is a regression, not the expected lift. Honest retrospective per `feedback_pre_conclusion_observation_checklist`.

## Per-worker delta

| Worker | B54 V | B55 V | Δ | Headline |
|---|---|---|---|---|
| W1 chat_router_smoke | 5/7 | 4/7 | **-1** | S4 word_stats V→R (spawn-ack reply, regression) |
| W2 stdlib_skills_core | 5/7 | 4/7 | **-1** | S2 skill_builder V→R (router crash), S5 R-2 partial, S6 R-3 INCOMPLETE |
| W3 control_ir_ops | 6/9 | 7/9 | **+1** | S6 lint R→V ✓ (R-6 hint worked); S7 I→I (env), S9 R→R (R-6 insufficient) |
| W4 permissions_and_safety | 8/8 | 6/8 | **-2** | S2 V→I (workspace arg missing), S6 V→R (LLM premature confirmation) |
| W5 multi_agent_and_mcp | 2/7 | 2/7 | 0 | R-5a NO LIFT (content loosened but events still fail) |
| W6 plan_mode | 4/5 | 4/5 | 0 | S3 R→V ✓ (W6-S3 fix verified), S1 V→R noise |
| W7 long_session_v1 | 6/7 | 7/7 | **+1** | S5 attractor did NOT recur (N=1→N=0, dismiss) |

## Per-fix verification (= 5 fixes promised, actual delivery)

| Fix | PR | Predicted | Actual | Status |
|---|---|---|---|---|
| R-2 eval postprocessor schema | #875 | W2-S5 R→V (+1V) | R→R (partial) | **PARTIAL** — fallback works in 1/3 attempts, but A2A reply returns before async eval completes (= different bottleneck, not fixed by R-2) |
| R-3 chat_compactor redesign | #880 | W2-S6 R→V (+1V) | R→R | **FAILED** — threshold=2000 inject did not trigger compactor; actual compactable tokens 47-71 across 5 turns. My token estimate was off by ~2 orders of magnitude. |
| W6-S3 prompt full-path | #883 | W6-S3 R→V (+1V) | R→V ✓ | **DELIVERED** — trace evidence confirmed deterministic full-path preservation in step descriptions |
| R-5a W5 rubric refocus | #912 | W5 +2-3V | 0V | **FAILED** — content rubric loosened but structural events (routing_decided / skill_run_spawned) also did not fire on weak-tier inline-reply path. Should have used must_emit_any for events too. |
| R-6 W3 S6/S7/S9 | #914 | W3 +1-2V | +1V (S6 only) | **PARTIAL** — S6 lint hint worked; S7 hint worked at LLM call layer but env error different (`missing_required_arg: sources`); S9 must_emit_any insufficient (skill_run_spawned + routing_decided also absent on inline path) |

**Net fix delivery**: +2V (W3-S6 + W6-S3) out of predicted +6-8V.

## Regressions (= -V not caused by fix-wave failure but by other causes)

5 regressions observed:

1. **W1-S4 word_stats_demo** V→R: reply was spawn-ack ("analysis started") instead of stats. Possible cause: PR #880 runner_grants compaction config injection cross-impact? (= compaction may have affected reply timing/structure on simple word_stats path). **Probable root cause**: compaction-config additive change had unforeseen side-effect on short scenarios where compactor partially fired mid-reply, or LLM noise.

2. **W2-S2 skill_builder_web_summariser** V→R: router crash with `"list index out of range"`. **Structural bug** — not LLM noise. Likely a regression in chat_router or skill_builder code path.

3. **W4-S2 mcp_install_gate_prompt** V→I: `skill_run_spawned` missing because `mcp install` tool_failed with workspace arg missing. **Infrastructure gap** — workspace arg might have been dropped between B54 and B55 (= cross-PR side effect).

4. **W4-S6 index_drop_destructive_gate** V→R: LLM said "I have started the drop" then corrected. **LLM behavioral regression** (= weak-tier noise).

5. **W6-S1 plan_compare_two_concepts** V→R: aggregation delivered paragraphs not 3+ discrete points. **LLM behavioral noise** OR W6-S2/S3/S4/S5 yaml changes (B54 + B55 wave) caused subtle shift in plan-mode behavior.

## V-rate decomposition (per `feedback_v_metric_decomposition`)

vs B54 (= 36/50):

| Component | Effect | Detail |
|---|---|---|
| Fix effect (= predicted) | +2 | W6-S3 + W3-S6 deterministic fixes delivered |
| Fix effect (= failed predicted) | 0 | R-2 / R-3 / R-5a / R-6 partial (S7+S9) did not translate to V |
| Regression — structural bug | -1 | W2-S2 router crash (pre-existing or cross-PR? need investigation) |
| Regression — infrastructure | -1 | W4-S2 workspace arg gap |
| LLM noise — content-only | -2 | W1-S4 / W4-S6 / W6-S1 (= weak-tier ±2V noise band) |
| Carry-over dismiss | +1 | W7-S5 sub-agent attractor N=1→N=0 (= dismissed) |
| **Net** | **-1V** | (actual was -2V, attribution gap of 1V = within noise band) |

Note: actual -2V is within decomposition uncertainty (-1V predicted from above + ±1V noise).

## Calibration analysis — why prediction overshot ~10V

1. **R-3 threshold miscalculation (= my biggest error)**:
   - I assumed 5 turns of Reyn architecture explanation would generate ~2000+ compactable tokens
   - Actual observation: compactable tokens 47-71 across 5 turns
   - Off by 2 orders of magnitude
   - Lesson: my "back-of-envelope" calculation was unverified speculation, not measurement
   - Should have: dispatched a smoke test with the threshold change BEFORE landing the PR

2. **R-5a partial-fix scope**:
   - I dropped content rubric strictness
   - But did NOT loosen events.must_emit (= routing_decided + skill_run_spawned)
   - Weak-tier inline-reply path fails both, not just content
   - Lesson: failure-mode categorization was incomplete — should have observed events_actual in trace before deciding which rubric layer to loosen

3. **R-2 partial-effect ignored**:
   - I tested postprocessor with valid fallback (= unit tests passed)
   - But did NOT observe full async eval lifecycle in B54 trace
   - A2A reply returns spawn-ack before eval completes (= different bottleneck)
   - Lesson: integration trace observation > unit test for dogfood fix verify

4. **R-6 S9 must_emit_any insufficient**:
   - I assumed inline-reply path would satisfy via chat_turn_completed_inline
   - But skill_run_spawned + routing_decided are still required in must_emit
   - Same R-5a error pattern repeated 1 PR later

5. **No regression baseline check before predicting +V**:
   - I predicted +V from fixes but did not factor in LLM noise band (±2V) AND structural regression risk from cross-PR changes
   - R-3's runner_grants compaction config injection plausibly caused W1-S4 cross-impact
   - Predicted +6-8V but did not subtract -2V noise — net should have been +4-6V even in best case

**Brier-style accuracy**: prediction +6-8V vs actual -2V = ~10V miss. Worst calibration miss in N batches.

## Cross-PR impact hypothesis — PR #880 compaction config

Likely cross-impact from R-3's runner_grants compaction config injection (= trigger=2000 / head=1 / tail=1 / min_batch=2):

- Compactor MAY fire on shorter scenarios than intended
- Even if not triggering, the lowered head/tail can affect chat history shape
- **W1-S4** spawn-ack-instead-of-stats reply MIGHT be from compactor partial-fire mid-reply

**Verification needed**: compare W1-S4 events_actual in B54 vs B55 traces. If compaction events fire in B55-W1-S4 but not B54-W1-S4 → confirmed cross-impact.

## Decisions

1. **R-3 fix incomplete** — must revisit. Either:
   - Drop threshold further (= 100? 500?), measure actual compaction trigger empirically, or
   - Revert R-3 (= remove compaction config injection from runner_grants) and accept W2-S6 as untestable in 5-turn scenario, or
   - Move chat_compactor test to a dedicated long-session worker (= 20+ turns)

2. **R-5a needs revisit** — events.must_emit_any must include the inline-reply path too. Same for R-6 S9.

3. **R-2 needs supplemental fix** — postprocessor fallback alone insufficient; async eval lifecycle reply timing needs separate fix.

4. **Cross-PR impact verification** — verify W1-S4 / W4 regressions are NOT caused by R-3 compaction config side-effect via trace inspection.

5. **Prediction discipline** — `feedback_minimize_speculation` violated. Should have verified each fix via trace-patch-replay BEFORE predicting +V (= W6-S3 was the only fix where I did this, and it's the one that delivered).

## Carry-over for B56+

- W2-S5 R-2 partial: investigate async eval reply timing
- W2-S6 R-3 incomplete: redesign threshold OR revert
- W5 5 scenarios: extend R-5a to events.must_emit_any
- W3-S9 ask_user: extend R-6 to drop must_emit hard requirement
- W1-S4 + W4-S2 + W4-S6 regressions: verify root cause (cross-PR vs noise)
- W2-S2 router crash: investigate "list index out of range" bug

## Discipline applied (= lessons learned)

- ✅ Honest retrospective per [[feedback_pre_conclusion_observation_checklist]] — prediction miss not glossed over
- ❌ [[feedback_minimize_speculation]] violated — predicted +V on 5 fixes without trace-verify on 4 of them
- ❌ [[feedback_verify_fix_via_replay_before_land]] partially violated — only W6-S3 had trace-patch-replay verify, others had unit tests / yaml validation only
- ✅ User param fixed vs B54 (= apples-to-apples maintained)
- ✅ Cross-batch pattern threshold for W7-S5 dismiss (N=1→N=0, no recur)

## Cost

~$5 (= 50 scenarios × sonnet 7-parallel). Predicted ROI: +6-8V → actual ROI: -2V. **Negative ROI** on this batch (= user's pre-batch +V doubt was warranted).

## Files

- `dogfood/batch_b55.yaml` — batch config
- `docs/deep-dives/journal/dogfood/2026-05-25-batch-55-5-fix-wave-verify/workers/results-worker-{1..7}.json` — per-worker raw output
- `docs/deep-dives/journal/dogfood/2026-05-25-batch-55-5-fix-wave-verify/aggregate.json` — combined aggregate
- This retrospective
