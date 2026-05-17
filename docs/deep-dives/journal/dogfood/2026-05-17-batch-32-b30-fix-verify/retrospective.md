# Batch 32 — Retrospective

> Fourth dogfood batch under FP-0036. First time the
> `feedback_iterative_replay_patch_disambiguation.md` discipline was
> applied at batch scale — and it caught the operator (= me) writing
> hypotheses as findings in B30. The lesson is structural, not
> incidental.

---

## 1. What this batch verified, what it didn't

### Verified

- **B30-NEW-1** (`hot_list_n` 10 → 16). 17-tool cold-start surface present across every worker that checked. The discoverability-truncation bug from B30 W2 is closed.
- **B30-NEW-2** (`skill__eval` seed). Worker 2 S7 directly invoked `skill__eval` (= no `skill__direct_llm_eval` hallucination). Worker 3 S6 likewise. Worker 5 S1 invoked `skill__mcp_search` from seed visibility.
- **B30-NEW-3** (`reyn/local/` wipe). No `list_comprehension_generator`-style cross-scenario contamination observed.
- **W3 ablation operational discipline**: ran the first batch-scale `llm_replay.py --patch` ablation. Two scenarios attributed with HIGH confidence (= S2 → B27-M2, S5 → B28-Q2 classification rule), one refuted my B30 hypothesis (= S4 was probabilistic N=1 noise, not B29-MED-3 plan-first shift).

### Not verified (= observed but pending action)

- **B23-PRE-1 description ambiguity widened by NEW-2** (W7 S1 regression). When `skill__eval` joined the seed alongside `skill__skill_builder` / `skill__skill_improver` / `skill__skill_importer`, the LLM correctly noted *"these tools all have the same description and input schema"* and refused on Reyn-internal questions. NEW-2 surfaced an existing problem rather than introducing one.
- **Async skill race conditions** (W3 S1, W2 S4/S7/S9, W6 fp_0011_*) — the `(answered)` injection / stdin close / cancellation patterns reproduce in B32 across multiple workers. Same root cause class as issue #52 (= B27-H4); the original H4 grace-window fix was strictly for the shutdown drain path, not for these intermediate-turn paths.
- **Double/triple dispatch** (W6) — reproduced in B30, reproduced in B32. Not addressed by any wave to date. Needs its own investigation.
- **The wipe recipe is leakier than it looks** — three independent contamination surfaces (= `reyn/local/`, `wal.jsonl`, `history.jsonl`) discovered within 3 batches. The pattern itself is the finding.

---

## 2. The ablation lesson

The user reminder in B30 — *"dogfood principle は忘れないでね"* — was a discipline correction. B32 was the test of whether the correction stuck.

The relevant memory passage:
> LLM 挙動 anomaly は findings draft 前に llm_replay --patch で iterative context 修正

Operating that for the W3 cluster:

| Hypothesis in B30 journal | Ablation finding |
|---|---|
| "B27-M2 file__grep drop caused S2 fall-back to file__list" | ✅ CONFIRMED (HIGH, 3/3 vs 3/3) |
| "B29-MED-3 cwd injection pushed LLM to plan-first on S4" | ❌ REFUTED (HIGH, probabilistic N=1 noise; plan presence makes no difference) |
| "B28-Q2 classification shift drove S5 verdict change" | ✅ CONFIRMED (HIGH, behaviour identical, only classification rule differs) |
| "B28-MED-1 seed reshaped LLM recall mental model on S7" | UNRESOLVED (more ablation needed) |

The B30 inference paragraph contained one verified, one refuted, two unverified. **Treating all four as "副作用" in the same breath was the error.** The cost of that error was the hours between B30 ship and B32 ablation — non-trivial but cheap relative to landing a "fix" for a non-existent regression.

This batch is the first time the discipline paid back in measurable lost-time-avoided. The retrospective lesson is to keep paying that cost.

### The N=1 verified bug

B28 W3 reported S2 as verified. The ablation showed: with `file__grep` absent (= the post-B27-M2 state, which was already true in B28), the LLM picks `file__list` with wrong args **3/3 times**. So B28's verified outcome was a lucky single run. The attractor was always present.

The implication for calibration: any verified count without N≥3 (preferably N≥5) is **calibration-grade, not shippable-grade**. The dogfood log's `outcome_prediction` bands should be recalibrated against B28+B30+B32 trajectory taken together, not against any single batch's verified count.

---

## 3. Process reflection

### What worked

- **Ablation parallel to retest**. 7 sonnet B32 + 1 ablation sonnet = 8 concurrent. The ablation result arrived alongside the worker reports so the aggregate phase could resolve attribution immediately. The right cadence for "regression-cluster present + structural fix wave landed" batches.
- **Observation-first phrasing held in mid-batch reports this batch**. After B30's correction, the inline summaries said "verdict shifted; ablation pending" rather than "fix X caused it." The hypothesis paragraphs were properly tagged. The discipline correction transferred.
- **Multiple independent reproduction = strong signal**. The `wal.jsonl` / `history.jsonl` wipe gap was reported by W1 / W2 / W5 / W6 independently. Four sonnet agents, four worker prompts, same finding. That's the kind of cross-worker convergence that signals a real OS issue rather than a worker artifact.

### What needs adjustment

- **The wipe recipe pattern is structural.** Every batch surfaces a new contamination class:
  - B27 → standard wipe of `.reyn/events/`
  - B30 → `reyn/local/`
  - B32 → `wal.jsonl` + `history.jsonl`
  Continuing to extend a manual checklist is not the right answer. The right answer is `reyn dogfood wipe <agent>` (= or equivalent) that knows the OS's stateful surfaces and resets them all. This becomes a B33 priority.
- **W6's scope is too large** (= 11 scenarios across 3 yaml files). Skipped findings.md prose in both B30 and B32. Action for B33: split W6's load across two workers.
- **Worker output format drift** — even with the "findings.md FIRST, incremental per scenario" instruction, some workers compose the prose at the end. The pattern that consistently produced inline prose: workers that explicitly noted "I will append a row per scenario to findings.md as I score." The phrasing change might be small but worth testing in B33.

### What surprised us

- **A "regression" can be a probabilistic verified result hiding a persistent attractor**. The B28→B30 narrative was *"we fixed bugs and verified rate dropped."* The ablation rewrites it: *"we fixed bugs and discovered the verified rate was inflated by lucky LLM choices."* The number went down because the measurement got more honest, not because the system got worse. This is a calibration story, not a regression story.
- **NEW-1+2 fix unblocked one issue and exposed another** (= seed visibility enabled `skill__eval` direct invocation, which surfaced the B23-PRE-1 description-collision class). Fixes ripple. Future fix waves should anticipate which adjacent latent issues a structural change might expose.
- **`reyn dogfood run` is still not the framework path used**. Three batches with the legacy stdin-pipe driver. Task #93 (verifier integration) is now squarely on the critical path for B33+ — without it, we can't dogfood the dogfood framework.

---

## 4. Cross-reference to memory

- `feedback_iterative_replay_patch_disambiguation.md` — applied at batch scale for the first time; the lesson held.
- `feedback_pre_conclusion_observation_checklist.md` — held in mid-batch reports.
- `feedback_minimize_speculation.md` — 1 fix / 1 verification / 1 ablation cadence enacted.
- `feedback_envelope_layer_fix.md` — §4.6 (args double-serialize) directly invokes this principle.
- `feedback_observe_before_speculate_llm.md` — the passive principle whose active trigger (= the checklist memory) fired this batch.

---

## 5. Fix wave priorities for B33

1. **Wipe recipe extension** (= task #98 → `reyn dogfood wipe` command, OR explicit doc + prompt-template update with wal.jsonl + history.jsonl). The structural answer is the command.
2. **B32 §4.1 file__grep**: implement routing rule + handler, OR add envelope-layer arg-hint when `file__list` is called with non-path args.
3. **B32 §4.4 skill description audit**: disambiguate `skill__eval` / `skill__skill_builder` / `skill__skill_improver` / `skill__skill_importer`. Modeled on B29's pair audit; this is a 4-way audit.
4. **B32 §4.6 args double-serialize**: defensive JSON-string detection at the invoke_action entry. Envelope-layer.
5. **B32 §4.2 + #52 async skill race**: router should not inject `(answered)` until spawned-this-turn skills reach terminal state.
6. **B32 §4.5 double-dispatch**: separate issue + investigation.
7. **task #93 verifier integration**: required to flip future batches to the `reyn dogfood run` path.
8. **Calibration recalibration**: B28+B30+B32 trajectory has enough data to set N=3 verified-rate bands per scenario, replacing the original single-batch outcome_prediction.

---

## 6. Goal restated

After four batches: **OS fix waves are landing cleanly when they target structural bugs**; **scenario-design and calibration discipline are where the leverage now lives**. The ablation discipline (= `feedback_iterative_replay_patch_disambiguation.md`) is the new floor for batch retrospectives. Future batches should not ship without ablation when verdict regression is observed.

Target for B33: ablation-confirmed fix for §4.1 (file__grep) + wipe recipe restructured to a command. Verified rate should clear 25% if §4.1 lands cleanly.
