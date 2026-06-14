# Scheme efficacy validation — methodology (staging design, for lead design-review)

**Author:** dogfood-coder (efficacy lead) · **Status:** design-review gate (lead) · **Effort:** the #1593 wave's WHY-validation.

Owner-approved effort: empirically test the premise the #1593 pluggable-scheme wave
rests on — *universal-category tool-use degrades for weak models; pluggable schemes
(CodeAct / enumerate-all / retrieval) recover capability.* This is design-only;
**no measurement run until lead's design-review clears it** (then run, ROI-gated).

---

## Hypothesis (to verify, not assume)

**H1 (core):** For a **weak** model, CodeAct ≥ universal-category on tool-use task
success (the weak model can't reliably navigate universal's qualified-name discovery
+ JSON-wrapper overhead; CodeAct's code-API + compute-then-call recovers it).

**H2 (contrast):** The gap **narrows/closes** for a **strong** model (confirms the
effect is weak-model-specific, not a universal-category defect at all tiers — i.e.
the WHY is "pluggable schemes for weak models," not "universal is just worse").

A null/negative result is a real outcome (it would re-frame the #1593 WHY) — the
measurement must be able to **falsify** H1.

## (a) Models — weak primary, strong contrast

- **Primary axis:** weak-tier `gemini-2.5-flash-lite` — where the scheme difference
  should manifest (the controlled comparison runs here).
- **Contrast/ceiling:** strong-tier `gemini-2.5-flash` — same task-set, to test H2
  (does the gap close at higher capability?).
- Both via the `localhost:4000` proxy. **All runs on the MAIN tree**
  (`PYTHONPATH=<user-main-worktree>/src`), per the #1609 worktree-drift gate — else
  `import reyn` may resolve a stale worktree and the measurement is invalid. Every
  result line states "run against main @ <sha>".

## (b) Task selection — scheme-differentiating, non-trivial

Tasks MUST (i) **require** tool use (not answerable from model knowledge — else no
tool-use signal), and (ii) tax the dimension schemes differ on (discovery / multi-step
/ data-dependent control flow). Candidate classes:
- **Discovery-required:** the right action must be found among many (universal:
  list_actions→describe_action→invoke; CodeAct: the code-API lists them inline) —
  stresses weak-model navigation.
- **Multi-tool sequence:** read → transform → write (3+ dependent calls) — stresses
  per-call JSON correctness (universal) vs in-code sequencing (CodeAct).
- **Data-dependent control flow:** call depends on a prior result (CodeAct's
  compute-then-call strength; universal needs a round-trip per branch).

Exclude trivial single-tool tasks (no scheme delta) + knowledge-answerable tasks (no
tool signal). **A fixed task-set, identical across schemes** (confound control).
*Design-review Q1: the specific task-set (curated dogfood set vs an existing tool-use
benchmark) — I'll propose a candidate ~8-12 task set for lead's approval.*

## (c) Scheme = the ONLY variable

Same task, same model, same prompt-frame, same sandbox, same N — **only
`tool_use.chat` varies** (universal-category / codeact / enumerate-all / retrieval).
**Faithful conditions:** no hot-list tuning, no per-scheme task hints, no soft-cheat —
each scheme advertises capability via the general path
([[feedback_benchmark_catalog_tuning_is_soft_cheat]]). The prompt-frame (system task
description) is identical; only the scheme's own SP/tools differ (which IS the variable
under test).

## (d) N + confound control

- **N ≥ 5 per (task × scheme × model)** — weak models are noisy; average per-task
  variance. (Design-review Q2: N — balance signal vs proxy cost.)
- Report **per-task** results + aggregate; never extrapolate a verdict from 1–2 runs
  ([[feedback_pre_conclusion_observation_checklist]], [[feedback_per_scenario_attractor_audit]]).
- Retry / re-plan paths are **part of** the scheme's behavior (not controlled out);
  the task/model/frame/N are fixed.
- **Trace-primary-evidence:** `dogfood_trace.py` for every run; the verdict comes from
  traces (the failure-mode WHY), not pass/fail counts alone
  ([[feedback_dogfood_trace_obligation]], [[feedback_event_type_only_extrapolation_trap]]).

## (e) Metric + verdict

- **Primary:** task-success rate per (scheme × model) — goal achieved, not exact-output
  match (Design-review Q3: success criterion).
- **Secondary (the WHY):** tool-call validity rate (valid/attempted), turns-to-completion,
  and a **failure-mode taxonomy** — *universal's qualified-name/JSON errors vs CodeAct's
  code/sandbox errors vs RePresent non-convergence.* The taxonomy is the real insight
  (WHY a scheme wins/loses for a weak model), more than the rate.
- **Verdict = INTERNAL signal** — PASS-rate NOT published
  ([[feedback_swe_passrate_internal_signal_not_published]]). It informs the #1593 WHY,
  not a published benchmark number; report faithfully (failures stated, N stated).

## Discipline gates (pre-conclusion)

- **No confounded numbers** — faithful conditions, scheme=only-variable
  ([[feedback_no_confounded_benchmark_number]] if present; [[feedback_benchmark_catalog_tuning_is_soft_cheat]]).
- **ROI-gate** — before the batch, trace-verify the task-set genuinely differentiates
  the schemes on weak-tier (a pre-run smoke on 1-2 tasks), else redesign the task-set
  ([[feedback_dogfood_batch_roi_gate]]).
- **Main-tree run** — all runs on main @ <sha> via PYTHONPATH (#1609); stated per result.
- **No-advance-until-structural-ruled-out** — a scheme's weak-tier failure is examined
  (structural-path defect vs genuine model-capability) before it counts as the scheme
  losing ([[feedback_no_advance_until_structural_ruled_out]], [[feedback_dogfood_uncovers_production_gap]]).

## Sequencing

1. **Foundational live-verify** (pre-measurement smoke): CodeAct drives flash-lite
   end-to-end (in-code `tool()` → per-call gate → result) on main tree — *mine*;
   retrieval(#1604)/enumerate(#1605) — *e2e*. Proves the schemes run live before the
   controlled comparison.
2. **This methodology** → lead design-review (the gate) → ROI-gate smoke → measurement run.
3. Verdict (internal) → the #1593 WHY-validation finding.

## Design-review questions (lead's gate)

- **Q1** task-set (curated dogfood vs existing benchmark; the ~8-12 candidate set).
- **Q2** N per cell.
- **Q3** success criterion (goal-achievement vs exact-output).
- **Q4** scope: all 4 schemes, or core CodeAct-vs-universal first + enumerate/retrieval
  as a second wave (retrieval needs the embedding class configured — available via the
  OPENAI key per e2e, but a setup step).

No run until this clears. e2e provides scheme-specific input (per-scheme correct-invoke
+ expected behavior) on the task-set + the per-scheme failure-mode taxonomy.
