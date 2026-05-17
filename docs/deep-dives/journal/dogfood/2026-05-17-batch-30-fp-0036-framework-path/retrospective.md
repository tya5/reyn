# Batch 30 — Retrospective

> Third dogfood batch under FP-0036. Verified-rate non-monotone for the
> first time in this sequence (B27 0 → B28 12 → B30 10). The non-monotone
> reading is the central retrospective topic: causal attribution
> discipline matters more here than fix throughput.

---

## 1. What this batch verified, what it didn't

### Verified

- **C1 hot-list filter** stable across 3 batches and 7 workers each (= W7 alone gives 52/52 turns clean). This fix is now boring — the right kind of boring.
- **B29-Q2 chat_turn_completed_inline + must_emit_any** semantics work as designed across every worker that exercised the path. The synthetic event mechanism is solid; remaining non-verifieds in §3 of findings.md are *rubric-bound*, not event-bound.
- **B28-NEW-2 (reyn.yaml python.safe)** holds in B30 (W2). Independent confirmation across two batches.

### Not verified (= regression observed, causal attribution pending)

- **W3 control_ir cluster lost 4 verified vs B28** (= S2/S4/S7/S8/S5 each changed verdict in the wrong direction). The mid-batch reflex was to attribute each change to one B27/B28/B29 fix that landed in the same merge wave. That attribution is **inference, not observation**. The data supports "verdict changed"; it does not support "fix X caused it".
- **B28-MED-1 (skill__index_docs seed) fix** is partial: the seed is present in source, but `hot_list_n=10` cap silently truncates it, so the LLM never sees the alias on cold start. Root cause identified by W2, fix candidate sketched in §4.1 of findings.md.
- **B29-MED-3 (PLAN-STEP-PATH cwd injection)** is partial in a different sense: the injection works, but the *step description generation* still produces bare filenames. Necessary-but-not-sufficient.

---

## 2. Process reflection

### The "fix X caused this" reflex

During the worker reports I wrote phrases like *"B29-MED-3 cwd injection の副作用で plan-first へ shift"* and *"B28-MED-1 seed の副作用で recall 理解変化"*. The user reminded me of the dogfood principle. Those phrases were **inference dressed as observation** — exactly the failure mode `feedback_pre_conclusion_observation_checklist.md` was written to prevent.

The corrective:
- A verdict regression is an observation: *scenario X moved from verified to refuted between batch N-1 and batch N.*
- The cause is a hypothesis until ablation runs.
- All B27/B28/B29 fixes landed in the same merge wave; their effects are **confounded** in B30's behaviour.

This batch is the first time the dogfood log has needed the `feedback_iterative_replay_patch_disambiguation.md` discipline (= use `scripts/llm_replay.py --patch` to undo one suspected change at a time and observe whether the regression reverts). Previous batches improved monotonically, so attribution was straightforward. B30 is the first batch where the technique becomes mandatory.

### Worker long-tail vs stuck

W5 ran ~27 min vs ~14 min batch average. The user surfaced "時間かかりすぎじゃない？" at ~25 min in. Considering B27's worker-E experience (where we killed and salvaged), this batch we discussed but did not kill. W5 eventually completed with substantive findings (= V/I/R/B 1/3/3/0, two Q2 verifications).

The heuristic that worked: **wait while output channels remain quiet but exit code uncertain; consider killing when the duration exceeds 2× batch median and no new evidence is arriving**. For B30 the right call was wait. For B27 worker E the right call was kill. The difference was the user's gut-check at the right moment.

### Framework path probe

`reyn dogfood run` ran cleanly through 7 scenarios in the smoke and produced summary.json. But the per-scenario `output.json` has `detail: {}` — verifier triad is not wired into the runtime even though it exists in the module. This is task #93. The dogfood batches keep running via the legacy stdin-pipe pattern as a result.

The framework-path probe is itself an observation: live_runner works for *driving* the agent, not yet for *scoring*. The remaining work is the verifier integration, not a redesign.

---

## 3. What we want to keep

- **Worker prompt verification angles** continue to surface structural fix evidence inline with each verdict. Without them, the W3 regression would have read as "5 scenarios regressed" rather than "5 distinct LLM behaviour shifts, each with primary-data evidence".
- **Findings-first incremental output**. 5/7 workers wrote findings.md prose this batch (= 6/7 in B28); the holdouts had richer results.json. The pattern is stable.
- **B28→B30 cross-batch comparison table**. Reading "B27 → B28 → B30" as a row per scenario surfaces regressions immediately. Keep this structure for B31+.

## 4. What needs adjustment

- **Observation-first phrasing in mid-batch reports**. Not just final journals. The mid-batch report is where the inference temptation is highest because data is incomplete and the user is waiting. Action: write mid-batch reports as observation-only ("scenario X moved verdict; cause TBD"). Save attribution for after ablation.
- **Replay-ablation as a routine retrospective step**. When verdict regresses, the default response should now be "queue an llm_replay --patch run for B(N-1) traces of the affected scenarios" rather than "write a hypothesis paragraph in the journal". Either build the ablation into the worker prompt for regression cases, or run it from the main agent post-aggregate.
- **`reyn/local/` between scenarios** (= W1 new finding). The worker wipe recipe template missed this directory. Update for B31.

## 5. What surprised us

- **B30's net -2V was the first non-monotone batch in this sequence**. The simple "fix wave → retest → improvement" loop had been working since B27. The regression doesn't invalidate the loop, but it does mean the loop's feedback isn't free — once the system gets non-trivial, fix waves can interact, and the dogfood-discipline becomes load-bearing rather than just careful.
- **C1 fix's longevity**. Three batches, dozens of distinct trace windows, hundreds of LLM calls, zero regressions. This is the bar for "shipped".
- **The discoverability vs disambiguation distinction** (= §4.1 / §4.2 / §4.3 in findings.md). MED-1 and the eval audit each individually addressed half of a two-layer problem. The seed has to *be there* AND the LLM has to *see it as a direct alias*. Either layer alone is insufficient — and the seed-presence test passed in `test_action_usage_tracker.py` while the discoverability layer silently broke. The lesson: tests on the seed contents are not tests on the LLM's effective context window.

---

## 6. Cross-reference to memory

- `feedback_pre_conclusion_observation_checklist.md` — the corrective applied mid-batch. Internalised better for B31.
- `feedback_minimize_speculation.md` — stop accumulating hypotheses; one fix, one verification, one next action.
- `feedback_iterative_replay_patch_disambiguation.md` — operational pattern for the W3 regression ablation.
- `feedback_envelope_layer_fix.md` — applicable to §4.7 (file__write KeyError → envelope-layer handler hardening).
- `feedback_observe_before_speculate_llm.md` — passive principle; the active trigger memory above is what fired this batch.

---

## 7. Fix wave priorities for B31

In priority order:

1. **B30-NEW-1 + NEW-2** (= hot_list_n bump + skill__eval seed). 5-line patch with invariant test. Highest expected V improvement per line of code.
2. **W3 regression ablation** (= scripts/llm_replay.py --patch on B28 traces for S2/S4/S7/S8/S5). Information-gathering, no source change. Should run *before* designing further fixes touching MED-1 / MED-3 / M2 — otherwise we risk piling speculation on speculation.
3. **B30-NEW-3 worker wipe recipe** (= include `reyn/local/`). 2-line change to the dogfood worker prompt template + the live_runner once §4 lands.
4. **`reyn dogfood run` verifier integration** (= task #93). Medium-scope. Unlocks batch dispatch via the framework path.
5. **Double-dispatch investigation** (= §4.4). Separate issue.

## 8. Goal restated

The arc from B27 to B30: OS-layer bugs resolved, scenario contract tightened, framework runner partially landed. The remaining work is now **LLM-attractor / discoverability** (= B30-NEW-1/2/3 cluster), **causal attribution discipline** (= ablation routine), and **framework verifier integration** (= #93). None are bigger than 1-2 sonnet-days. The system is ready for B31 to push the verified rate above 50% if the §4 fixes land cleanly and the W3 cause is identified.
