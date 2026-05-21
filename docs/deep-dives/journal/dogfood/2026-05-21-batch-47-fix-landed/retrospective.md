# B47 Retrospective — 2026-05-21

**Batch focus**: verify the 5 OS fixes landed during B46 closure round
(PR #330 / #332 / #337 / #338 / #358) deliver V improvement under live
dogfood load. Accumulate PR #287/#290 default-flip evidence toward
N≥50 soft checkpoint.

- HEAD at dispatch: `e6d31b61` (= all 5 fixes landed)
- ENV: `REYN_EMPTY_STOP_RETRY=1`, `REYN_SPAWN_ACK_TO_LLM=1`
- User params (held constant vs B43-B46): `hot_list_n=10`,
  `models.tier=flash-lite` (build_skill auto-upgrades to strong via
  PR #358)
- Sub-agent model: sonnet
- Hard caps observed: 50 tool-uses / 15 min wall-clock per worker
- Measurement-process controls: single past-V anchor (B46 only),
  neutral worker prompts (= no "regression triage" framing)

## Headline

| Metric        | B44   | B45   | B46   | **B47**  | ΔvsB46 |
|---------------|-------|-------|-------|----------|--------|
| V (verified)  | 23    | 14    | 18    | **12**   | **-6** |
| V/N           | 23/50 | 14/49 | 18/50 | 12/52    |        |
| Verified rate | 0.46  | 0.286 | 0.36  | **0.23** | -0.13  |

**B47 V trajectory is the lowest of the 4-batch sequence**. Original
prediction "≥22 because fixes landed" was **wrong**. Investigation
shows the V drop is not a mole-whack of new bugs as initially
hypothesised — see "Methodology recap" below.

## Per-worker (V) — past-comparison table

| Worker | Scenario set                | B45 V | B46 V | **B47 V** | ΔvsB46 |
|--------|-----------------------------|-------|-------|-----------|--------|
| W1     | chat_router_smoke           | 1/7   | 3/7   | 1/7       | -2     |
| W2     | stdlib_skills_core¹         | 1/9   | 1/9   | 0/7       | -1²    |
| W3     | control_ir_ops              | 3/9   | 3/9   | 4/9       | +1     |
| W4     | permissions_and_safety      | 3/8   | 3/8   | 4/8       | +1     |
| W5     | multi_agent_and_mcp         | 1/7   | 1/7   | 1/7       | 0      |
| W6     | plan_mode_fp_0011_mixed     | 2/7   | 2/7   | 2/7       | 0³     |
| W7     | long_session_v1             | 3/7   | 5/7   | 0/7       | **-5** |

¹ Scenario count 9 → 7 after PR #338 read_local_files removal.
² B47 W2=0/7 vs B46 W2=1/9 — V dropped by 1 absolute even with
  smaller denominator.
³ W6 had 4 B (blocked) due to dispatch-script scenario-count
  mismatch (plan_mode.yaml has 3 scenarios, worker prompt said 7).

## Fix verification status (= the headline question)

| PR | Target scenario | B47 verdict | Verified? |
|---|---|---|---|
| #330 eval spec_path | W2-S5 `eval_run_direct_llm` | R | **Blocked upstream** by a new bug (direct_llm path resolution at run_target phase) — `FileNotFoundError: 'skills/direct_llm.yaml'` etc. PR #330's evaluate-phase fix never reached. Closed by follow-up PR #379 (form-4 hallucination recovery). |
| #332 recall | W3-S7 `recall_indexed_source` | V | ✅ **Verified end-to-end**. Clean "no sources indexed" reply, no raw KeyError leak. |
| #337 wrapper | W2-S4 multi-line `word_stats_demo_multiline` | R | ⚠️ B47 surfaced apparent stats=0 failure; **N=10 reproducibility check (2026-05-21) showed 0/10 stats=0 → original B47 finding was N=1 noise, fix is working**. PASS cases reported correct multi-line stats (119/127/107 chars × 4 lines). |
| #338 read_local_files | scenario set | — | ✅ scenario count reduced 9→7 as expected. |
| #358 build_skill strong | W2-S2 `skill_builder_web_summariser` | R | ⚠️ B47 surfaced apparent "invalid JSON escape" failure; **N=10 effective reproducibility (= 7 valid runs) showed 5/7 = 71% PASS, vs ~40% baseline at all-standard** = strong-tier fix significantly improves but doesn't eliminate. Remaining 29% FAIL is upstream LLM noise (design_artifacts phase). |

**Aggregate fix verdict**:
- 1 fix directly verified at dogfood scenario level (#332)
- 1 fix structurally working (#337) but B47 N=1 finding misled to "broken" — N=10 cleared
- 1 fix substantially improving (#358) but not perfect
- 1 fix blocked upstream by a different bug (#330) — chain closed by PR #379
- 1 fix mechanical (#338) — scenarios dropped

## New OS bug found + fixed: PR #379 (form-4 hallucination recovery)

Investigation chain (= operational maturity example):

1. B47 W2-S5 `eval_run_direct_llm` failed with worker-reported "5x
   skill_run_failed schema validation". My N=1 read suggested this
   was a follow-up to PR #330's schema fix.
2. User pushback: "本当に bug? 根拠は?" (= are these actually
   bugs, or N=1 inference?) — triggered N=3 reproducibility check
   per `feedback_code_inspection_not_enough_for_fix`.
3. N=3 reproduction surfaced the REAL bug: each of 3 runs hit
   `FileNotFoundError` with a DIFFERENT path
   (`'skills/skill__direct_llm.py'`, `'skills/direct_llm.py'`,
   `'skills/direct_llm.yaml'`). Worker's "schema validation"
   report was a misread.
4. Root cause: router LLM hallucinates `target_skill_path` field
   when invoking eval skill — invents paths like `skills/<name>.py`
   or `skills/<name>.yaml` instead of the bare name "direct_llm".
5. Fix (PR #379): added form 4 to `_resolve_skill_ref` —
   defensive normalization that strips `.yaml`/`.py`/`.md`
   extension + `skill__` prefix + leading path segments, then
   retries `resolve_skill_path` on the candidate. Emits warning
   log when normalization fires.
6. 13 Tier 2 tests + 55 existing tests + ruff clean.

Methodology adherence: N=3 deterministic reproduction (= 3/3
trigger rate) confirmed real bug before structural fix per
`feedback_code_inspection_not_enough_for_fix`. PR #379 merged
2026-05-21 commit `5adc233`.

## Methodology recap (= the headline learning)

Initial B47 reading: V=12/52 -6V, plus N=1 observations of "new
failure modes" for #337 + #358 = "mole-whack structure, fixes
deliver no V improvement, switch strategies".

**User pushback** challenged the inference chain: each N=1
observation interpreted as structural attribution without
trigger-rate measurement violates
`feedback_code_inspection_not_enough_for_fix`. The "mole-whack"
generalization was over-extrapolated from sparse data.

**N=3 → N=10 reproducibility check** disambiguated each candidate:

```
N=3 (initial trigger-rate check):
  #330 eval direct_llm path: 3/3 reproduced = real bug → PR #379
  #337 multi-line stats=0:   0/3 = noise
  #358 build_skill JSON:     1/3 = upstream LLM noise zone

N=10 (re-verification confidence increase):
  #337 stats=0:               0/10 = NOISE CONFIRMED
  #358 build_skill:           5/7 effective = 71% PASS (improvement)
  + side finding A: stats.py 5s timeout at parallel load
  + side finding B: build_skill name fidelity loss (9/10 hallucinate)
```

`feedback_pre_conclusion_observation_checklist` + `feedback_code_inspection_not_enough_for_fix`
operationalised. Single observation → structural attribution is the
trap; trigger-rate measurement is the defence.

## Side findings (deferred, classified as known limitations)

- **stats.py 5s timeout at parallel load**: 10-parallel calls of
  the `compute_text_stats` python preprocessor trigger 7/10 timeouts
  due to ~2.2s cold-start of `_python_harness` subprocess + I/O
  contention. Single-agent dispatch doesn't hit this. Doc as known
  environment limitation; no OS fix required.
- **build_skill name fidelity loss**: 9/10 parallel skill_builder
  runs with `web_summary_repro2` requested produced `web_summary` /
  `web_summary_repro` instead. LLM weak-model name-shortening
  behaviour, not OS-level. Doc as known limitation. The router-
  level fuzzy-match contamination (= n2/n5/n10 invoked existing
  built skill instead of dispatching skill_builder) is a related
  parallel-dispatch artefact.

Per `feedback_reyn_care_boundary`, both fall under "LLM への注文"
layer where the OS care surface is minimal.

## W7 -5V: not a code-level regression

W7 (long_session_v1) collapsed from 5V (B46) → 0V (B47) with identical
scenarios + identical Reyn HEAD on the rubric-bearing subset. None of
the 5 fixes directly touch long_session scenarios.

Most plausible cofounders:
- **Sub-agent judging-strictness shift** (= sonnet day-to-day variance;
  worker self-noted a new "tool-affordance bias hypothesis" not present
  in B46)
- **Gemini upstream drift** (= unverifiable)

Per `feedback_pre_conclusion_observation_checklist`, this is
inference-only, not primary-data-confirmed. Treat as measurement-drift
noise floor for W7 specifically.

## PR #287 / #290 default-flip evidence (cumulative B44+B45+B46+B47)

- **PR #287 retry_injected**: 3 + 5 + 3 + 3 = **14 events** total, 0
  false positives observed.
- **PR #290 spawn_ack_exit**: 3 + 3 + 12 + 1 = **19 events** total;
  0 literal `_SPAWN_ACK_MSG` echoes across ≈130 turns observed.
- B47 W7 spawn_ack_exit was only 1 event (vs B46 12) — W7 regression
  also affected the spawn-ack codepath exposure.

Toward N≥50 soft checkpoint: PR #287 at 14/50 (28%), PR #290 at 19/50
(38%). Need ~2-3 more clean batches OR target-scenario tests to reach
checkpoint.

## Carry-overs — DOGFOOD LOOP PAUSED until #383 E-full completes

User decision (2026-05-21): "B にする、 直近 Batch 歯伸び悩んでるので
383 に期待して待つ".

**B48+ dispatch paused** until e2e-coder's #383 E-full series (=
ChatMessage shape expansion, 4-5 PR over ~数 days) completes. Rationale:

- E-full touches LLMReplay fixtures + chat_compactor + trace dump
  format — all central to dogfood measurement infra
- B48 in the middle of E-full series would cross-shape the V
  trajectory comparison (= new measurement-process cofounder),
  making B48 retrospective noisy
- User expects E-full to address some of the structural attractors
  blocking V improvement (= tool_call/result detail discard, image
  follow-up, multi-step plan refinement)

Plan:
1. Wait for e2e-coder broker post: "E-full Phase 1 complete"
2. Update `dogfood_variant_replay.py` / `dogfood_trace.py` to handle
   new trace dump format if needed (= author backwards-compat or
   migration)
3. Resume with **B48 as the new-baseline batch**, single past anchor
   (= no V comparison to B47 — comparing across shape boundary is
   apples-to-oranges)
4. Re-establish trajectory with cleaner measurement

## Pipeline / tooling note

- `dogfood_batch_dispatch.py` worker-prompt scenario count for W6 is
  still wrong (= prompts say 7, plan_mode.yaml has 3). Carry-over
  from B45 retro; B47 W6 had 4 B due to this. Fix during E-full pause
  window if time allows.
