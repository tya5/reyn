#!/usr/bin/env python3
"""
H5 Brier calibration analysis.

Dataset:
  - Predictions: outcome_prediction from dogfood/scenarios/*.yaml
  - Actuals: verdict from results-worker-*.json for B27, B28, B30, B32

Note: fp_0011_narration.yaml and fp_0011_0012_retest.yaml do NOT have
outcome_prediction fields per scenario; they are older spike-format files.
They are covered by worker 6 in each batch.

Worker 6 covers both plan_mode.yaml, fp_0011_narration.yaml, and
fp_0011_0012_retest.yaml scenarios. Only those with outcome_prediction
in the YAML can be included in calibration.

Scenarios with outcome_prediction across 9 YAML files:
  chat_router_smoke: 7 scenarios
  stdlib_skills_core: 9 scenarios
  control_ir_ops: 9 scenarios
  permissions_and_safety: 8 scenarios
  multi_agent_and_mcp: 7 scenarios
  plan_mode: 3 scenarios
  fp_0011_narration: no outcome_prediction fields (spike format)
  fp_0011_0012_retest: no outcome_prediction fields (spike format)
  long_session_v1: 2 scenarios (only scenario_1 and scenario_5 have expected/outcome_prediction)

Total: 7+9+9+8+7+3+2 = 45 scenarios with predictions.

Brier score (multi-class):
  BS = (1/N) * sum_over_outcomes (p_outcome - a_outcome)^2
  where a_outcome is 1 if actual==outcome else 0.

Per-scenario with 4-batch actuals:
  actual_freq[outcome] = count(outcome in B27,B28,B30,B32) / 4

Refitted prediction = actual frequency (= 4-batch empirical distribution).
Refitted Brier = 0 for the correct outcome in the refitted model (it assigns
probability 1 to the most common outcome if all 4 agree).

Actually the correct Brier for refit is computed as:
  refit_p[outcome] = actual_freq[outcome]
  refit_brier = sum((refit_p[o] - a_o)^2)

But wait — for Brier under refit we need to compute it against all 4 observations
independently (not the mean). The proper approach is to compute per-batch Brier
for both original and refit, then average.

Method used:
  For each scenario, for each batch run:
    brier_orig = sum_o (pred[o] - indicator(actual==o))^2
    For refit: refit_pred[o] = (count(o in 4 batches) / 4)
    brier_refit = sum_o (refit_pred[o] - indicator(actual==o))^2
  Mean over all (scenario, batch) pairs.

Bands: verified (V), inconclusive (I), refuted (R), blocked (B)
"""

import csv
import json
from pathlib import Path

OUTCOMES = ["verified", "inconclusive", "refuted", "blocked"]

# ─── Scenario predictions ──────────────────────────────────────────────────────
# Manually transcribed from YAML files

predictions = {
    # chat_router_smoke (worker 1)
    "simple_capability_question":         {"V": 0.70, "I": 0.20, "R": 0.05, "B": 0.05},
    "factual_query_direct_llm":           {"V": 0.75, "I": 0.18, "R": 0.05, "B": 0.02},
    "skill_discovery_request":            {"V": 0.65, "I": 0.25, "R": 0.05, "B": 0.05},
    "explicit_skill_invocation_word_stats":{"V": 0.60, "I": 0.28, "R": 0.07, "B": 0.05},
    "catalog_routing_decided_emitted":    {"V": 0.65, "I": 0.25, "R": 0.05, "B": 0.05},
    "multi_turn_pronoun_reference":       {"V": 0.60, "I": 0.28, "R": 0.07, "B": 0.05},
    "out_of_scope_graceful_decline":      {"V": 0.65, "I": 0.25, "R": 0.07, "B": 0.03},

    # stdlib_skills_core (worker 2)
    "index_docs_basic":                   {"V": 0.55, "I": 0.30, "R": 0.10, "B": 0.05},
    "read_local_files_explain_source":    {"V": 0.55, "I": 0.30, "R": 0.10, "B": 0.05},
    "read_local_files_multi_file":        {"V": 0.50, "I": 0.33, "R": 0.12, "B": 0.05},
    "skill_builder_web_summariser":       {"V": 0.55, "I": 0.28, "R": 0.12, "B": 0.05},
    "word_stats_demo_sentence":           {"V": 0.65, "I": 0.25, "R": 0.05, "B": 0.05},
    "word_stats_demo_multiline":          {"V": 0.65, "I": 0.25, "R": 0.05, "B": 0.05},
    "eval_run_direct_llm":                {"V": 0.50, "I": 0.33, "R": 0.12, "B": 0.05},
    "chat_compactor_long_session":        {"V": 0.45, "I": 0.40, "R": 0.10, "B": 0.05},
    "chained_find_then_index":            {"V": 0.50, "I": 0.33, "R": 0.12, "B": 0.05},

    # control_ir_ops (worker 3)
    "file_read_via_chat":                 {"V": 0.65, "I": 0.25, "R": 0.05, "B": 0.05},
    "file_glob_grep":                     {"V": 0.60, "I": 0.30, "R": 0.05, "B": 0.05},
    "web_search_query":                   {"V": 0.65, "I": 0.25, "R": 0.05, "B": 0.05},
    "web_fetch_url":                      {"V": 0.60, "I": 0.25, "R": 0.05, "B": 0.10},
    "sandboxed_exec_simple":              {"V": 0.55, "I": 0.30, "R": 0.05, "B": 0.10},
    "lint_a_skill":                       {"V": 0.60, "I": 0.30, "R": 0.05, "B": 0.05},
    "recall_indexed_source":              {"V": 0.30, "I": 0.55, "R": 0.05, "B": 0.10},
    "judge_output_direct":                {"V": 0.55, "I": 0.30, "R": 0.10, "B": 0.05},
    "ask_user_round_trip":                {"V": 0.40, "I": 0.40, "R": 0.10, "B": 0.10},

    # permissions_and_safety (worker 4)
    "file_write_outside_cwd_denied":      {"V": 0.45, "I": 0.40, "R": 0.10, "B": 0.05},
    "mcp_install_gate_prompt":            {"V": 0.30, "I": 0.50, "R": 0.10, "B": 0.10},
    "sandbox_seatbelt_denied_network":    {"V": 0.30, "I": 0.50, "R": 0.10, "B": 0.10},
    "credential_scope_intersection":      {"V": 0.25, "I": 0.55, "R": 0.10, "B": 0.10},
    "budget_chain_warn_checkpoint":       {"V": 0.25, "I": 0.55, "R": 0.10, "B": 0.10},
    "index_drop_destructive_gate":        {"V": 0.30, "I": 0.50, "R": 0.10, "B": 0.10},
    "shell_disallowed_by_default":        {"V": 0.35, "I": 0.45, "R": 0.15, "B": 0.05},
    "web_fetch_denied_by_config":         {"V": 0.40, "I": 0.45, "R": 0.10, "B": 0.05},

    # multi_agent_and_mcp (worker 5)
    "mcp_search_registry":               {"V": 0.60, "I": 0.30, "R": 0.05, "B": 0.05},
    "mcp_call_remote_tool":              {"V": 0.20, "I": 0.55, "R": 0.05, "B": 0.20},
    "agent_delegation_simple":           {"V": 0.30, "I": 0.45, "R": 0.05, "B": 0.20},
    "multi_agent_topology_route":        {"V": 0.20, "I": 0.45, "R": 0.05, "B": 0.30},
    "a2a_task_lifecycle_status_poll":    {"V": 0.50, "I": 0.35, "R": 0.05, "B": 0.10},
    "mcp_install_permission_gate":       {"V": 0.40, "I": 0.40, "R": 0.05, "B": 0.15},
    "cron_schedule_status":              {"V": 0.40, "I": 0.45, "R": 0.05, "B": 0.10},

    # plan_mode (worker 6 partial)
    "plan_compare_two_concepts":         {"V": 0.45, "I": 0.35, "R": 0.15, "B": 0.05},
    "plan_explain_with_code_references": {"V": 0.40, "I": 0.35, "R": 0.20, "B": 0.05},
    "plan_summary_across_n_files":       {"V": 0.35, "I": 0.40, "R": 0.20, "B": 0.05},

    # fp_0011_narration (worker 6, scenarios with predictions embedded in batch results)
    # These do NOT have outcome_prediction in the YAML, so excluded.
    # narr-1-mcp-search, narr-3-skill-builder → excluded

    # fp_0011_0012_retest (worker 6, scenarios without outcome_prediction) → excluded
    # s-fp11-1-builder-invalid-spec, etc → excluded

    # long_session_v1 (worker 7, only 2 have outcome_prediction)
    "scenario_1_reyn_research_chain":    {"V": 0.65, "I": 0.25, "R": 0.05, "B": 0.05},
    "scenario_5_general_python_chain":   {"V": 0.70, "I": 0.20, "R": 0.05, "B": 0.05},
}

# ─── Actuals from batch results ────────────────────────────────────────────────
# Transcribed from the JSON files above.
# Format: {scenario_id: {"B27": verdict, "B28": verdict, "B30": verdict, "B32": verdict}}
# Using first-letter: V=verified, I=inconclusive, R=refuted, B=blocked

actuals = {
    # chat_router_smoke (W1 in each batch)
    "simple_capability_question":           {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "V"},
    "factual_query_direct_llm":             {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "V"},
    "skill_discovery_request":              {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "R"},
    "explicit_skill_invocation_word_stats": {"B27": "B",  "B28": "R",  "B30": "I",  "B32": "V"},
    "catalog_routing_decided_emitted":      {"B27": "B",  "B28": "R",  "B30": "R",  "B32": "R"},
    "multi_turn_pronoun_reference":         {"B27": "B",  "B28": "R",  "B30": "R",  "B32": "V"},
    "out_of_scope_graceful_decline":        {"B27": "B",  "B28": "R",  "B30": "R",  "B32": "R"},

    # stdlib_skills_core (W2)
    "index_docs_basic":                     {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "R"},
    "read_local_files_explain_source":      {"B27": "I",  "B28": "R",  "B30": "R",  "B32": "V"},
    "read_local_files_multi_file":          {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "R"},
    "skill_builder_web_summariser":         {"B27": "I",  "B28": "V",  "B30": "V",  "B32": "I"},
    "word_stats_demo_sentence":             {"B27": "B",  "B28": "R",  "B30": "I",  "B32": "I"},
    "word_stats_demo_multiline":            {"B27": "R",  "B28": "R",  "B30": "I",  "B32": "I"},
    "eval_run_direct_llm":                  {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "I"},
    "chat_compactor_long_session":          {"B27": "R",  "B28": "R",  "B30": "I",  "B32": "I"},
    "chained_find_then_index":              {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "R"},

    # control_ir_ops (W3)
    "file_read_via_chat":                   {"B27": "I",  "B28": "V",  "B30": "V",  "B32": "R"},
    "file_glob_grep":                       {"B27": "R",  "B28": "V",  "B30": "R",  "B32": "R"},
    "web_search_query":                     {"B27": "I",  "B28": "V",  "B30": "V",  "B32": "V"},
    "web_fetch_url":                        {"B27": "I",  "B28": "V",  "B30": "R",  "B32": "V"},
    "sandboxed_exec_simple":                {"B27": "R",  "B28": "I",  "B30": "R",  "B32": "R"},
    "lint_a_skill":                         {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "R"},
    "recall_indexed_source":                {"B27": "B",  "B28": "V",  "B30": "R",  "B32": "I"},
    "judge_output_direct":                  {"B27": "B",  "B28": "V",  "B30": "R",  "B32": "R"},
    "ask_user_round_trip":                  {"B27": "B",  "B28": "R",  "B30": "R",  "B32": "R"},

    # permissions_and_safety (W4)
    "file_write_outside_cwd_denied":        {"B27": "I",  "B28": "I",  "B30": "I",  "B32": "I"},
    "mcp_install_gate_prompt":              {"B27": "I",  "B28": "V",  "B30": "V",  "B32": "I"},
    "sandbox_seatbelt_denied_network":      {"B27": "I",  "B28": "V",  "B30": "I",  "B32": "I"},
    "credential_scope_intersection":        {"B27": "I",  "B28": "I",  "B30": "V",  "B32": "V"},
    "budget_chain_warn_checkpoint":         {"B27": "I",  "B28": "I",  "B30": "I",  "B32": "I"},
    "index_drop_destructive_gate":          {"B27": "I",  "B28": "I",  "B30": "V",  "B32": "V"},
    "shell_disallowed_by_default":          {"B27": "I",  "B28": "I",  "B30": "V",  "B32": "V"},
    "web_fetch_denied_by_config":           {"B27": "R",  "B28": "V",  "B30": "R",  "B32": "R"},

    # multi_agent_and_mcp (W5)
    "mcp_search_registry":                  {"B27": "I",  "B28": "R",  "B30": "I",  "B32": "I"},
    "mcp_call_remote_tool":                 {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "I"},
    "agent_delegation_simple":              {"B27": "R",  "B28": "I",  "B30": "I",  "B32": "I"},
    "multi_agent_topology_route":           {"B27": "R",  "B28": "I",  "B30": "V",  "B32": "I"},
    "a2a_task_lifecycle_status_poll":       {"B27": "R",  "B28": "R",  "B30": "R",  "B32": "R"},
    "mcp_install_permission_gate":          {"B27": "R",  "B28": "R",  "B30": "I",  "B32": "R"},
    "cron_schedule_status":                 {"B27": "R",  "B28": "I",  "B30": "R",  "B32": "R"},

    # plan_mode (W6)
    "plan_compare_two_concepts":            {"B27": "I",  "B28": "I",  "B30": "I",  "B32": "V"},
    "plan_explain_with_code_references":    {"B27": "B",  "B28": "I",  "B30": "R",  "B32": "R"},
    "plan_summary_across_n_files":          {"B27": "B",  "B28": "I",  "B30": "I",  "B32": "R"},

    # long_session_v1 (W7) — only 2 with predictions
    # B30/B32 use raw_band=inconclusive / true_band (rubric-based); using true_band here.
    # B30 W7: true_band scenario_1=verified, scenario_5=verified
    # B32 W7: true_band scenario_1=refuted, scenario_5=verified
    "scenario_1_reyn_research_chain":       {"B27": "R",  "B28": "V",  "B30": "V",  "B32": "R"},
    "scenario_5_general_python_chain":      {"B27": "R",  "B28": "V",  "B30": "V",  "B32": "V"},
}

VERDICT_MAP = {"V": "verified", "I": "inconclusive", "R": "refuted", "B": "blocked"}
PRED_KEY = {"verified": "V", "inconclusive": "I", "refuted": "R", "blocked": "B"}

def brier_score(pred_dict, actual_verdict):
    """Multi-class Brier score for one observation."""
    bs = 0.0
    for o in ["V", "I", "R", "B"]:
        p = pred_dict[o]
        a = 1.0 if actual_verdict == o else 0.0
        bs += (p - a) ** 2
    return bs

# Compute per-scenario stats
records = []
for sid in predictions:
    pred = predictions[sid]
    acts = actuals[sid]

    # 4-batch frequencies
    freq = {"V": 0, "I": 0, "R": 0, "B": 0}
    for batch, v in acts.items():
        freq[v] += 1
    refit = {k: v / 4.0 for k, v in freq.items()}

    # Per-batch Brier scores
    orig_briers = []
    refit_briers = []
    for batch, v in acts.items():
        orig_briers.append(brier_score(pred, v))
        refit_briers.append(brier_score(refit, v))

    mean_orig = sum(orig_briers) / len(orig_briers)
    mean_refit = sum(refit_briers) / len(refit_briers)
    improvement = mean_orig - mean_refit  # positive = refit is better

    records.append({
        "scenario": sid,
        "pred_V": pred["V"], "pred_I": pred["I"], "pred_R": pred["R"], "pred_B": pred["B"],
        "act_V": freq["V"]/4, "act_I": freq["I"]/4, "act_R": freq["R"]/4, "act_B": freq["B"]/4,
        "brier_orig": mean_orig,
        "brier_refit": mean_refit,
        "improvement": improvement,
        "actuals": [acts["B27"], acts["B28"], acts["B30"], acts["B32"]],
    })

# Sort by improvement (most miscalibrated first = most improvement under refit)
records.sort(key=lambda r: r["improvement"], reverse=True)

# Aggregate stats
mean_orig = sum(r["brier_orig"] for r in records) / len(records)
mean_refit = sum(r["brier_refit"] for r in records) / len(records)
n_big_improvement = sum(1 for r in records if r["improvement"] > 0.3)

# Per-band drift
mean_pred_V = sum(r["pred_V"] for r in records) / len(records)
mean_pred_I = sum(r["pred_I"] for r in records) / len(records)
mean_pred_R = sum(r["pred_R"] for r in records) / len(records)
mean_pred_B = sum(r["pred_B"] for r in records) / len(records)

mean_act_V = sum(r["act_V"] for r in records) / len(records)
mean_act_I = sum(r["act_I"] for r in records) / len(records)
mean_act_R = sum(r["act_R"] for r in records) / len(records)
mean_act_B = sum(r["act_B"] for r in records) / len(records)

drift_V = mean_act_V - mean_pred_V
drift_I = mean_act_I - mean_pred_I
drift_R = mean_act_R - mean_pred_R
drift_B = mean_act_B - mean_pred_B

print(f"N scenarios: {len(records)}")
print(f"Mean Brier (original): {mean_orig:.4f}")
print(f"Mean Brier (refit):    {mean_refit:.4f}")
print(f"Improvement (orig-refit): {mean_orig - mean_refit:.4f}")
print(f"N scenarios with >0.3 improvement: {n_big_improvement}")
print()
print(f"Mean predicted V: {mean_pred_V:.3f}  Actual: {mean_act_V:.3f}  Drift: {drift_V:+.3f}")
print(f"Mean predicted I: {mean_pred_I:.3f}  Actual: {mean_act_I:.3f}  Drift: {drift_I:+.3f}")
print(f"Mean predicted R: {mean_pred_R:.3f}  Actual: {mean_act_R:.3f}  Drift: {drift_R:+.3f}")
print(f"Mean predicted B: {mean_pred_B:.3f}  Actual: {mean_act_B:.3f}  Drift: {drift_B:+.3f}")
print()
print("Top 10 most miscalibrated (orig Brier highest):")
top10 = sorted(records, key=lambda r: r["brier_orig"], reverse=True)[:10]
for r in top10:
    print(f"  {r['scenario'][:45]:45s}  orig={r['brier_orig']:.3f}  refit={r['brier_refit']:.3f}  "
          f"impr={r['improvement']:.3f}  acts={r['actuals']}")

# Write CSV
csv_path = "/tmp/reyn-ablation/H5-calibration/scenarios_brier.csv"
with open(csv_path, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["scenario", "pred_V", "pred_I", "pred_R", "pred_B",
                     "act_V", "act_I", "act_R", "act_B",
                     "brier_orig", "brier_refit", "improvement",
                     "B27", "B28", "B30", "B32"])
    for r in records:
        writer.writerow([
            r["scenario"],
            f"{r['pred_V']:.3f}", f"{r['pred_I']:.3f}", f"{r['pred_R']:.3f}", f"{r['pred_B']:.3f}",
            f"{r['act_V']:.3f}", f"{r['act_I']:.3f}", f"{r['act_R']:.3f}", f"{r['act_B']:.3f}",
            f"{r['brier_orig']:.4f}", f"{r['brier_refit']:.4f}", f"{r['improvement']:.4f}",
            r['actuals'][0], r['actuals'][1], r['actuals'][2], r['actuals'][3],
        ])
print(f"\nCSV written to {csv_path}")

# Store results for report
results = {
    "n_scenarios": len(records),
    "mean_orig": mean_orig,
    "mean_refit": mean_refit,
    "improvement": mean_orig - mean_refit,
    "n_big_improvement": n_big_improvement,
    "mean_pred_V": mean_pred_V, "mean_act_V": mean_act_V, "drift_V": drift_V,
    "mean_pred_I": mean_pred_I, "mean_act_I": mean_act_I, "drift_I": drift_I,
    "mean_pred_R": mean_pred_R, "mean_act_R": mean_act_R, "drift_R": drift_R,
    "mean_pred_B": mean_pred_B, "mean_act_B": mean_act_B, "drift_B": drift_B,
    "top10_by_brier_orig": [{
        "scenario": r["scenario"],
        "pred_V": r["pred_V"], "act_V": r["act_V"],
        "brier_orig": r["brier_orig"], "brier_refit": r["brier_refit"],
        "improvement": r["improvement"],
        "actuals": r["actuals"],
    } for r in top10],
    "top10_by_improvement": [{
        "scenario": r["scenario"],
        "pred_V": r["pred_V"], "act_V": r["act_V"],
        "brier_orig": r["brier_orig"], "brier_refit": r["brier_refit"],
        "improvement": r["improvement"],
        "actuals": r["actuals"],
    } for r in records[:10]],
    "all_records": records,
}

with open("/tmp/reyn-ablation/H5-calibration/results.json", "w") as f:
    json.dump(results, f, indent=2)
print("JSON written to /tmp/reyn-ablation/H5-calibration/results.json")
