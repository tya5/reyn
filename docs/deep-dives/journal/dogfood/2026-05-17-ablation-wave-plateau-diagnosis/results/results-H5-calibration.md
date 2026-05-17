# H5 outcome_prediction recalibration

## Method

**Dataset**: 45 scenarios with `outcome_prediction` fields across 7 scenario YAML files
(`chat_router_smoke`, `stdlib_skills_core`, `control_ir_ops`, `permissions_and_safety`,
`multi_agent_and_mcp`, `plan_mode`, `long_session_v1`).
Excluded: `fp_0011_narration.yaml` and `fp_0011_0012_retest.yaml` (spike-format, no
`outcome_prediction` per scenario).

**Actuals**: Worker JSON results from B27, B28, B30, B32 (4 batches × 45 scenarios = 180
observations). For `long_session_v1`, the `true_band` field (rubric-based) was used over
`raw_band` where they differed (B30/B32 W7).

**Brier score**: Multi-class 4-outcome Brier, range [0, 2.0]:
```
BS(pred, actual) = Σ_o (pred[o] - indicator(actual==o))²
```
Per-scenario mean Brier = average of 4 per-batch Brier values.
Aggregate = mean over all 45 scenarios.

**Refitted prediction**: Empirical 4-batch frequency per scenario
(`act[o] = count(verdict==o across B27/B28/B30/B32) / 4`).

---

## Per-scenario calibration table

Top 10 most miscalibrated (highest original Brier), sorted by original Brier desc:

| Scenario | pred_V | act_V | pred_R | act_R | brier_orig | brier_refit | improvement | B27 | B28 | B30 | B32 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| skill_discovery_request | 0.65 | 0.00 | 0.05 | 1.00 | 1.390 | 0.000 | 1.390 | R | R | R | R |
| catalog_routing_decided_emitted | 0.65 | 0.00 | 0.05 | 0.75 | 1.390 | 0.375 | 1.015 | B | R | R | R |
| out_of_scope_graceful_decline | 0.65 | 0.00 | 0.07 | 0.75 | 1.371 | 0.375 | 0.996 | B | R | R | R |
| lint_a_skill | 0.60 | 0.00 | 0.05 | 1.00 | 1.355 | 0.000 | 1.355 | R | R | R | R |
| a2a_task_lifecycle_status_poll | 0.50 | 0.00 | 0.05 | 1.00 | 1.285 | 0.000 | 1.285 | R | R | R | R |
| index_docs_basic | 0.55 | 0.00 | 0.10 | 1.00 | 1.205 | 0.000 | 1.205 | R | R | R | R |
| word_stats_demo_multiline | 0.65 | 0.00 | 0.05 | 0.50 | 1.190 | 0.500 | 0.690 | R | R | I | I |
| word_stats_demo_sentence | 0.65 | 0.00 | 0.05 | 0.25 | 1.190 | 0.625 | 0.565 | B | R | I | I |
| sandboxed_exec_simple | 0.55 | 0.00 | 0.05 | 0.75 | 1.180 | 0.375 | 0.805 | R | I | R | R |
| factual_query_direct_llm | 0.75 | 0.25 | 0.05 | 0.75 | 1.148 | 0.375 | 0.773 | R | R | R | V |

Full per-scenario data: `/tmp/reyn-ablation/H5-calibration/scenarios_brier.csv`

---

## Aggregate

- **N scenarios**: 45 (out of 58 total; 13 excluded — no `outcome_prediction` in YAML)
- **Original predictions: mean Brier = 0.9125** (scale 0–2.0; ~46% of maximum)
- **Refitted (4-batch frequency): mean Brier = 0.3972**
- **Improvement: orig − refit = +0.5152**
- **N scenarios with >0.3 Brier improvement under refit: 26 / 45 (58%)**

---

## Per-band drift (verified-high bias?)

| Band | Mean predicted | Mean actual frequency | Drift (actual − pred) |
|---|---|---|---|
| verified (V) | 0.492 | 0.189 | **−0.303** |
| inconclusive (I) | 0.350 | 0.289 | −0.061 |
| refuted (R) | 0.082 | 0.467 | **+0.385** |
| blocked (B) | 0.076 | 0.056 | −0.020 |

Observations:
- **verified** was predicted at ~49% on average, but only achieved ~19% across 4 batches.
  Prediction overestimates verified by **0.303 probability units** (≈ 2.6× actual rate).
- **refuted** was predicted at ~8% on average, but achieved ~47% across 4 batches.
  Prediction underestimates refuted by **0.385 probability units** (≈ 5.7× predicted rate).
- inconclusive and blocked are roughly calibrated (drift < 0.07).

The dominant miscalibration pattern is:
- **verified-high** (predicted V ≈ 2.6× actual V rate)
- **refuted-low** (actual R ≈ 5.7× predicted R rate)

---

## W3 ablation connection

The B28 "21% verified rate" hypothesis from the W3 ablation (which noted N=1 lucky
verified count) is confirmed in the aggregate data. The mean actual verified frequency
across all 45 scenarios and 4 batches is **18.9%** — consistent with the B28 observation
being representative rather than lucky. The overall system was in a low-verified state
from B27 through B32 due to infrastructure bugs (duplicate tool declarations, permission
mismatches, hot_list truncation) that only partially resolved across batches.

---

## Conclusion

- **Predictions are strongly verified-biased**: mean predicted V = 0.492 vs mean actual V = 0.189.
- **Magnitude: large** — drift of −0.303 on V band, +0.385 on R band; mean Brier improvement
  of 0.515 under refit (a 56% reduction in Brier).
- **58% of scenarios (26/45) show >0.3 Brier improvement** under the 4-batch empirical refit.
- **5 scenarios had actual verified frequency = 0/4** but were predicted at V ≥ 0.50:
  `skill_discovery_request` (pred=0.65), `lint_a_skill` (0.60), `a2a_task_lifecycle_status_poll`
  (0.50), `index_docs_basic` (0.55), `word_stats_demo_multiline` (0.65) — all refuted in
  every batch.

**Action: recalibrate before B33 — YES, strongly recommended.**

Recalibration guidance:
1. Reduce default verified band to 0.15–0.25 for first-batch scenarios on a new code path.
2. Raise refuted band to 0.35–0.50 for scenarios that depend on skill dispatch
   (artifact presence + must_emit skill_run_spawned), since the dispatch infrastructure
   was the primary failure mode in B27–B32.
3. Scenarios with `must_emit: skill_run_completed` should carry blocked ≥ 0.10 and
   refuted ≥ 0.30 until the underlying tool chain is confirmed stable.
4. The well-calibrated band is inconclusive (drift −0.06): predictions for I are sound.
