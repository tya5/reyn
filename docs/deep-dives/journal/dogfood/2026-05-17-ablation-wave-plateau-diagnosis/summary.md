# Ablation wave — plateau diagnosis (2026-05-17)

> First-of-its-kind batch-scale ablation under `feedback_iterative_replay_patch_disambiguation.md`. Seven hypotheses about the dogfood verified-rate plateau (~19% across B27-B32) ran in parallel sonnet worktrees. Result: two decisive findings (H5 + H3), four "not-bound" dismissals, one secondary contribution (H2). Total ablation cost: ~30 min wall-clock + a single strong-model probe (= H1, capped scope per the user's "strong is expensive" constraint).

---

## 0. Wave summary

| Item | Value |
|---|---|
| HEAD pre-wave | `8bf3305` (= post-B32 journal) |
| Hypotheses ablated | 7 (H1-H7) |
| Sonnet workers | 7 parallel, isolated worktrees |
| Wall-clock | ~30 min (= longest H1 strong-model ~27 min, longest patch-based H3/H4 ~16 min) |
| Strong-model usage | 1 set probe only (H1, 16 scenarios × N=3) — minimized per constraint |
| Decisive findings | **H5 (calibration bias)** + **H3 (`(answered)` race)** |
| Dismissed (not-bound) | H4 (description), H6 (wipe), H7 (reorder) |
| Mixed / partial | H2 (rubric, 14-27%), H1 (model, not the ceiling) |

---

## 1. Per-hypothesis verdict matrix

| H | Hypothesis | Method | Flip rate / Δ | Conclusion |
|---|---|---|---|---|
| H1 | flash-lite probabilistic non-compliance bounds plateau | gemini-2.5-flash on 16 scenarios × N=3 vs B32 flash-lite | -10.4pp | **NOT model-bound**; race-class fails same way on strong model |
| H2 | judge rubric wording too tight | 5 refuted re-judged under semantic-loose variants | 3/5 (selection-bias); projected 14-27% | **Secondary driver**, not plateau ceiling |
| H3 | `(answered)` injection race on async skill | patch router_loop to exit on spawn-ack, replay affected scenarios | 1/22 directly attributed | **OS-layer structural fix**; same root mechanism observed in H1 strong model |
| H4 | 4-way skill description ambiguity | description disambiguator patch + N=3-5 replay | 0/4 | **NOT description-bound**; hot-list visibility + dispatch envelope are the real layers |
| H5 | outcome_prediction verified-high biased | recompute Brier under B27-B32 4-batch frequency | 0.913 → 0.397 (56% reduction); ΔV = -0.30 | **DECISIVE: predictions wrong, not OS wrong** |
| H6 | wipe-recipe gap (wal.jsonl / history.jsonl) | full wipe + replay 7 contamination-suspect scenarios | 1/7 = 14% | **NOT wipe-bound** (= hygiene priority, not plateau driver) |
| H7 | action_usage reorder evicts seed entries | analytic + replay multi-turn scenarios | 0/0 patch-gate not met | **NOT reorder-bound** (structurally impossible post-B30-NEW-1) |

---

## 2. Decisive findings

### 2.1 H5 — outcome_prediction verified-high biased (DECISIVE)

**Primary data** (= 45 scenarios with `outcome_prediction` × B27/B28/B30/B32 actuals = 180 outcome observations):
- Mean predicted verified: **0.492**
- Mean actual verified frequency: **0.189** (Δ = -0.303)
- Mean predicted refuted: **0.082**
- Mean actual refuted frequency: **0.467** (Δ = +0.385)
- Mean Brier (original predictions): 0.913
- Mean Brier (refitted from 4-batch frequency): 0.397
- 26/45 scenarios show >0.3 Brier improvement under refit

**Egregious miscalibration** (= predicted V ≥ 0.50, actual V = 0/4 batches):
- `skill_discovery_request` (predicted V=0.65)
- `lint_a_skill` (0.60)
- `a2a_task_lifecycle_status_poll` (0.50)
- `index_docs_basic` (0.55)
- `word_stats_demo_multiline` (0.65)

**Implication**: the "plateau" narrative from B27→B32 was partly a measurement artifact. The system was in a genuinely low-verified state due to discoverable bugs (= duplicate tool decl, hot-list truncation, permission key mismatch) which we fixed cleanly across waves. The verified rate's residual ~19% level is approximately the **true performance** of the system on these scenarios under flash-lite; predictions had set the expectation ~25 pp too high.

**Action (= landed in this wave)**: H5-refit sonnet running in parallel to apply the per-scenario refitted values to `dogfood/scenarios/*.yaml`. Will land as a separate commit.

### 2.2 H3 — `(answered)` injection race on async skill (OS structural)

**Primary data**:
- H3 patch: `src/reyn/chat/router_loop.py` — detect `invoke_skill` / `invoke_action` spawn-ack and exit the router loop instead of continuing the iteration.
- Race mechanism: spawn-ack accumulated as `role=tool` → `(answered)` workaround in `llm.py:821` injects a synthetic user prompt → LLM composes a final reply before the skill output arrives → generic "Understood" / "I will notify you" hallucination.
- Affected scenarios (= 1/22 batch-scale flip, but structural elimination of a recurring class): W3 S1 `file_read_via_chat`, W6 spawn-ack hallucination s-fp12-completion-2, similar patterns elsewhere.

**Cross-confirmation by H1 strong-model probe**: under `gemini-2.5-flash`, the same `(answered)` hallucination reproduces on 3/3 runs of file_read_via_chat / web_search_query / recall_indexed_source — confirming this is OS-layer, not LLM compliance. A stronger model picks the right tool, then gets the same wrong final reply because the injection happens at the OS / message-flow layer.

**Patch artifact**: `results/H3-patch.diff` (= 56 lines, applied to `main` as part of this wave's land — commit `25834de`).

---

## 3. Dismissed hypotheses (= not-bound)

### 3.1 H4 — 4-way skill description ambiguity (NOT bound)

Hypothesis: skill_builder / skill_improver / skill_importer / skill__eval description collisions cause wrong-skill invocation refuteds.

Ablation: patched all 4 descriptions with B29-style decisive verbs. Replayed 4 scenarios N=3-5 each. **0/4 flipped.**

The non-flip cases revealed the real layer:
- W2 S7 eval misroute: `skill__eval` was the **right** target post-B29 audit but the LLM couldn't see it under cold start — that's a hot-list visibility issue (= B30-NEW-2 addressed and confirmed in B32 W2), not a description issue.
- W6 double-dispatch: per-turn dedup gap + session carryover — envelope-layer / state-layer, not description.
- W7 S1 ambiguity complaint: N=1 probabilistic; non-reproducible at N=5 under fresh start.

Memory `feedback_envelope_layer_fix.md`'s prediction held: the failures live at the envelope (hot-list / dispatch / session) layer.

### 3.2 H6 — wipe gap (NOT bound at plateau scale)

Hypothesis: missing `wal.jsonl` / `history.jsonl` wipes pollute scenarios.

Ablation: applied the full wipe recipe (= include both files), replayed 7 contamination-suspect scenarios. **1/7 flipped** (`plan_summary_across_n_files`).

Wipe-gap-attributable to plateau: ~1/22 of B32 refuteds = ~4.5%. Hygiene matters (= task #98 still HIGH for operator-day quality), but it doesn't move the plateau.

### 3.3 H7 — action_usage reorder (NOT bound, structurally impossible post-B30-NEW-1)

Hypothesis: freq+recency reordering between turns demotes seed entries below `hot_list_n`.

Ablation: B30-NEW-1 bumped `hot_list_n` 10→16, while `DEFAULT_HOT_LIST_SEED` is 13 entries. **All 13 seed entries remain visible regardless of reordering** — no eviction can happen. Primary data from W7 B32 long_session: 51/51 router turns show both `skill__index_docs` and `skill__eval` visible.

H7 patch gate not triggered. The hypothesis would become live if `hot_list_n < len(SEED)` or the seed grows past `n`. The invariant test added in B30-NEW-1 (`test_default_seed_fits_within_default_hot_list_n`) protects against the future of that scenario.

---

## 4. Mixed / partial findings

### 4.1 H2 — judge rubric tightness (secondary driver)

Selection-biased sample of 5: 3 flipped under semantic-loose rubric variants. Projecting to all 22 B32 refuteds: ~14-27% accessible to rubric loosening. Three tightness patterns identified:
1. **Capability-assumption rubrics** ("describes files in X" fails on honest "cannot access" replies)
2. **Protocol-specificity rubrics** ("mentions GET /a2a/tasks/{run_id}" too literal)
3. **Rubric ambiguity** (LLM judge and human disagree on same rubric — W1 S7 `out_of_scope_graceful_decline`)

11/22 refuteds (= 50%) are event-driven only (= must_emit failures) and immune to rubric work. The remaining ~10 are mixed or rubric-accessible.

Action: defer rubric loosening to a focused per-scenario audit. Not in this wave.

### 4.2 H1 — strong model ceiling (NOT the bound, but informative)

`gemini-2.5-flash` on 16 scenarios × N=3:
- Δ = -10.4 pp vs flash-lite B32 baseline (= strong model **lower** verified rate on the sampled scenarios, partly because N=3 averages out flash-lite's lucky single runs)
- chat_router_smoke simple scenarios: 3/3 V under both models (= not the bottleneck)
- control_ir_ops W3 cluster under strong model: 1V/27 = 3.7% — the `(answered)` race fails identically

**The strong model probe paid for itself once**: it gave us the cross-confirmation that H3's race is OS-layer, not LLM compliance. We will not repeat it routinely (= user constraint on strong-model cost). Future ablations stay flash-lite + context-analysis + patch-based diagnosis.

---

## 5. The discipline that mattered

The user's reminder in B30 — *"dogfood principle は忘れないでね"* — was operationalised in this wave. Two specific patterns held:

### 5.1 No mid-wave hypothesis paragraphs

The journal pages for each ablation worker contain **observations and the patch / diff that tested them**, not narrative speculation. The aggregate (= this document) is the first place causal claims appear, and each cites a specific K/N or Brier delta.

### 5.2 Ablation precedes fix-design

Two examples concretely:
- I wrote in B30 *"B29-MED-3 cwd injection pushed LLM toward plan-first"*. H1 P-H1 patch (= remove `plan` from tools array) showed 3/3 baseline vs 3/3 patched on S4 — the LLM picks `invoke_action(web__fetch)` either way. The B30 hypothesis was wrong; the ablation cost was ~30 min.
- I would have proposed a "skill description 4-way audit" as a B33 fix. H4 ablation found 0/4 flipped under that patch. The cost of running that fix wave without ablation would have been hours of sonnet work plus a B33 retest. Ablation saved both.

The cost-benefit: ~30 min wall-clock for 7 parallel ablations to prevent ~10x that in misdirected fix waves.

### 5.3 SP / spec changes deferred

The user constraint *"SP 変更だけは慎重に"* held: no ablation patch touched system prompts. H3 patch is envelope-layer (router_loop event flow). H4 was schema-layer description audit but **did not flip**, so we don't land it. The H5 refit modifies scenario YAML predictions only, not any prompt or schema field.

---

## 6. What this wave changes about the project narrative

**Before B32 aggregate**: the dogfood verified-rate plateau ~19% read as "Reyn has a plateau ceiling that incremental fixes can't break." Multiple fix waves had landed without much ΔV.

**After this ablation wave**: ~19% is **the actual system performance** under flash-lite, on a scenario set whose `outcome_prediction` bands were authored ~30 pp too optimistic. The fix waves did land their structural fixes correctly; the residual is mostly outside reach of either small fixes or model upgrades:
- ~50% of refuted scenarios are `must_emit` failures whose scenarios assumed dispatch paths the router doesn't take (= partly addressable by `must_emit_any` extensions, partly by routing rule gaps for `file__grep` / `mcp_install` / etc.)
- ~14-27% are rubric-tightness flips waiting for a per-scenario audit
- ~5% are wipe-recipe artifacts
- The plateau is not a wall; it's a sum of small things, none of which is dominant, and the predictions that suggested otherwise were the loudest source of "plateau" perception.

This is a substantive re-framing. The B33+ work shifts from "find the breakthrough" to "land the H3 race fix + apply the H5 refit + work the long tail of small-scenario specifics."

---

## 7. Landed in this wave + next steps

### Landed

- **H3 patch** → `25834de` (`fix(router): exit on invoke_skill spawn-ack to break (answered) race`). Tests updated (= old behaviour contract on `test_user_message_invoke_skill_e2e` reflected the pre-patch race). 3346 passed.

### Landing (= H5 refit sonnet in progress)

- **H5 refit** → branch `refit/h5-predictions`. 45 scenarios' `outcome_prediction` updated to 4-batch frequency. Will land in this wave's window.

### Deferred

- **H2 rubric loosening**: per-scenario audit, ~10 scenarios. B33 candidate.
- **task #98** wipe recipe → `reyn dogfood wipe` command (= structural answer per H6 conclusion).
- **task #93** verifier integration (= still gapped, framework path not e2e).
- **B27-H4** (= issue #52) async skill `acompletion never awaited`: H3 fix is adjacent but does not address it. Separate work.

---

## 8. Cross-reference

- Per-hypothesis raw results: `results/results-H{1..7}-*.md`
- H3 patch artifact: `results/H3-patch.diff`
- H4 patch artifact (not landed): `results/H4-patch.diff`
- H5 refit data: `results/H5-scenarios_brier.csv` + `results/H5-compute.py`
- Memory applied this wave:
  - `feedback_iterative_replay_patch_disambiguation.md` (= operating principle)
  - `feedback_pre_conclusion_observation_checklist.md` (= no hypothesis-as-finding)
  - `feedback_minimize_speculation.md` (= 1 hypothesis / 1 ablation / 1 conclusion)
  - `feedback_envelope_layer_fix.md` (= H4 dismissal corroborated the layer-priority principle)
- B30 / B32 journals: `docs/deep-dives/journal/dogfood/2026-05-17-batch-{30,32}-*/`
- Commits: B30-NEW-1/2/3 (`67e21e3`, `c8fae2e`), H3 (`25834de`), H5-refit (`<pending>`).
