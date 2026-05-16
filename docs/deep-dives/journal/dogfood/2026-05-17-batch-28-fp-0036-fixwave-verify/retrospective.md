# Batch 28 — Retrospective

> First fix-wave verification batch under the FP-0036 framework. Same 58
> scenarios, same parallel pattern, same trace tooling — only the OS
> changed. Result: verified 0 → 12, refuted 26 → 21, blocked 13 → 1.
> What worked: the C1/H1/H2/H3/Q1/M2/NEW-2 fixes verified cleanly without
> any scenario-side guidance. What didn't: scenario-design assumptions
> (= B28-Q2) and a fresh band of LLM attractors remain.

---

## 1. What this batch verified, what it didn't

### Verified

- **Every Wave 1 fix landed correctly e2e.** C1 across all 7 workers, H1 / H2 / H3 each on the worker that originally found the bug. The retrospective principle "verify-first / reproduce-first" (= memory `feedback_verify_reproduce_first.md`) held: nothing was claimed fixed without primary-data evidence.
- **The 7-sonnet parallel + per-cwd + per-reyn-agent isolation pattern is stable.** Two batches in a row, zero state collision, ~30 min wall-clock for 58 scenarios.
- **Mid-batch fix is possible**. W1 surfaced the python.safe / python.pure config mismatch; main agent fixed reyn.yaml during W2's run; W2 independently corroborated. Total elapsed: <5 min from surface to e2e re-verification across two workers.

### Not verified

- **The FP-0036 framework's `reyn dogfood run` end-to-end path** — same as B27. Workers still ran via direct `reyn chat --cui` stdin pipe. The framework's `run` / `compare` / `publish` chain still has no real upstream until `_build_live_runner` is wired.
- **B27-7-BUG-2** (`reyn__source__read` hallucination from W7) — did not manifest in B28. Either the C1 fix changed the LLM's behaviour (= C1 confound dismissed), or N=1 was noise. Targeted reproduction would need a deliberate scenario. Defer to issue #54.
- **Issue #53 (web enforcement)** — W4 S8 confirmed the bug is active but did not fix it (= scoped out to its own issue).

---

## 2. What changed since B27 and how those changes mapped to findings

| Change | B27 finding it addressed | B28 outcome |
|---|---|---|
| `c0d5ea8` C1 hot-list filter | duplicate function declaration (6/7 workers) | ✅ verified, 62/62 long-session turns clean |
| `ef0a07f` H1 plan restore | plan-mode non-functional | ✅ plan tool present, plan_emitted 2/3 |
| `bceee51` H2 #49 revert | web__fetch visibility | ✅ web__fetch in tools array even under deny |
| `e17f6df` H3 peer-agent fix | KeyError: 'request' | ✅ delegate dispatched, no KeyError |
| `a8e7d34` H4 grace window | skill_run_interrupted on shutdown | ✅ partial — shutdown path verified, deeper bug remains (#52) |
| `32b28a0` Q1 scenarios refactor | skill_run_spawned mismatch | ✅ scenarios that exercise inline ops emit routing_decided cleanly |
| `1636584` M2 file__grep drop | UnknownActionError on hot-list use | ✅ file__grep never invoked |
| `f5a6866` S6 seed file__list / reyn.source__list | filesystem listing catch-22 | ⏳ no scenario directly stressed this in B28; deferred to B29 verification |
| `1a5be83` reyn.yaml python.safe | preprocessor permission denial | ✅ verified mid-batch by W2 |

### Where the verified-rate sits

- 0/58 → 12/58 = **20.7% verified**, +5 percentage points per fix-applied basis
- Of the remaining 46 (= I + R), the residual breakdown is roughly:
  - **~18 scenario-design (B28-Q2)** — must_emit doesn't match the LLM-chose-not-to-invoke path
  - **~10 LLM attractor / hallucination** — RAG, eval-vs-skill_improver, plan step path, etc.
  - **~10 authoring polish** — partial-expected scenarios, dotted-cwd handling
  - **~5 environment** — MCP registry, sandbox backend, peer agent absent
  - **~3 unclear / mixed**

The headline is no longer "Reyn is broken." It's "scenarios + LLM compliance need tightening, OS path is solid."

---

## 3. Process reflection

### What we want to keep

1. **Verification-angle prompts**. Asking each worker to explicitly check C1 / H1 / H2 / H3 / routing_decided per scenario forced primary-data evidence to surface immediately. Without this, "the fix works" would have been just a count diff — with it, we have specific trace excerpts behind each verified line.
2. **Mid-batch fix capability**. The python.safe issue was found, source-verified, fixed, and re-confirmed all within the same batch. This is the right rhythm — don't defer obvious config fixes to a "next wave" when the evidence is in front of you.
3. **Findings-first output**. 6/7 workers wrote findings.md incrementally. The one holdout (W6) had a richer results.json than B27, so the policy adjustment paid off even when not fully complied with.

### What needs adjustment

1. **Workers still skipping findings.md prose**. W6 dropped to results.json only. Recommendation: in B29, ask workers to also append a single-line per-scenario summary to a log file so the main agent always has a verbose-enough trail for synthesis.
2. **`reyn dogfood run` framework path still unused**. Two batches in, the framework's intended runner has yet to be invoked. The legacy `dogfood_b24_driver` pattern works but doesn't exercise: `_build_live_runner` → `runner.py` → `report.json` emission. Implementing `_build_live_runner` is now overdue.
3. **Scenario calibration is stale**. Predictions in the YAML were authored under assumptions that no longer hold. Both Brier scoring and calibration analysis were skipped this batch. Recalibration should be a B29 prep task.

### What surprised us

- **C1's blast radius was larger than expected**. B27 marked 13 scenarios blocked; B28 unblocked all 13 with a single 10-line filter. Even non-blocked scenarios in B27 had been silently degrading (e.g. W3's 3 refuted → 6 verified just from the C1 fix making subsequent turns work).
- **B28-Q2 pre-flight refusal pattern is more common than the scenario set anticipated**. ~18 of 46 non-verified outcomes trace to "LLM correctly declined; scenario expected a tool call." This means scenarios were authored with an inline-op bias even after the Q1 refactor.
- **Mid-batch user observability**. The user's "B27-H4 時間かかりすぎじゃない？" intervention in the previous batch and "進めて" / "再開して" prompts in this one created a useful rhythm: short main-agent reports → user gut-check → continued autonomous work. The protocol matches `feedback_dogfood_driver_role.md` (= Claude drives, user reviews); ratifying it explicitly in batch playbooks would help future operators.

---

## 4. Cross-reference to memory

- `feedback_envelope_layer_fix.md` — C1 fix is again an envelope-layer win: filter the tools array, don't touch SP.
- `feedback_verify_reproduce_first.md` — every fix verified e2e in this batch; no fix-without-observation.
- `feedback_observe_before_speculate_llm.md` — every finding cites primary-data evidence (trace excerpt / events.jsonl line).
- `feedback_minimize_speculation.md` — held: 1 fix → 1 verification → 1 next-action.
- `feedback_pre_conclusion_observation_checklist.md` — applied per worker; no "100% / decisive" claims without N/N inspection.

---

## 5. Fix wave priorities for B29

In priority order:

1. **B28-Q2 scenario contract decision** — read FP-0034 §D + router_loop emit logic, then decide between (a) emitting a synthetic `chat_turn_completed_inline` event when the router exits without invoking a tool, OR (b) relaxing scenarios that don't require a tool call to allow rubric-only verification. This is the single largest leverage on verified-rate.
2. **B28-MED-1 RAG attractor** — seed `skill__index_docs` + audit other indexing skills for hot-list coverage.
3. **B28-MED-2 eval ↔ skill_improver disambiguation** — description audit.
4. **B28-MED-3 PLAN-STEP-PATH** — inject resolved cwd into plan-step system prompts.
5. **`_build_live_runner` implementation** — the FP-0036 framework's missing piece. After it lands, B29 should run via `reyn dogfood run` (= dogfooding the dogfood framework itself).
6. **Calibration refresh** — recalibrate outcome_prediction bands per scenario using B28 actuals + the new contract from (1).
7. **Issues #52 / #53 / #54** — B27 follow-ups (root-cause `acompletion never awaited`, web enforcement, qualified-name multi-provider).

---

## 6. Goal restated

The trajectory after batches 23–28: **OS-layer is solid → scenario / LLM-attractor work is the remaining bottleneck → framework runner is overdue**. B29 closes the framework-runner gap and pivots into LLM-attractor / scenario-contract work. The OS-vs-skill / spec-vs-bug discipline (= dogfood-discipline §A5) keeps the decision boundary clean.
