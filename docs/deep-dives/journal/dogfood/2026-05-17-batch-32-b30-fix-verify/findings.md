# Batch 32 — Findings (B30-NEW-1/2/3 verification + W3 ablation)

> Fourth dogfood batch under FP-0036. Same 58 scenarios. Main HEAD
> `c8fae2e` (= post-B30-NEW-1/2/3 + B27/B28/B29 waves). Run in parallel
> with the W3 regression ablation (= memory
> `feedback_iterative_replay_patch_disambiguation.md`).
>
> Headline:
> - **B32 aggregate V/I/R/B = 11/24/22/0** (= +1V vs B30, -2R, blocked
>   stays 0 — sustained).
> - NEW-1 / NEW-2 / NEW-3 all verified e2e across the workers that
>   exercised them.
> - **W3 ablation resolved the B30 hypothesis**: the S2 regression is
>   causally attributed to **B27-M2** (not to B28-MED-1 / B29-MED-3 as
>   the mid-batch B30 inference had speculated). The B28 W3 verified
>   count was a lucky N=1; the persistent attractor was always there.

---

## 0. Run summary

| Item | Value |
|---|---|
| HEAD | `c8fae2e` (post-B30-NEW-1/2/3) |
| Tests post-rebase | 3340 passed / 5 skipped / 2 xfailed |
| Total scenarios | 58 |
| Workers | 7 sonnet parallel + 1 ablation sonnet (= 8 in flight) |
| Worktrees | `/tmp/reyn-worktrees/b32-{1..7}` |
| Wall-clock | ~30 min |
| Driver | legacy `reyn chat --cui` stdin pipe (= task #93 framework verifier still gapped) |
| **Aggregate** | **V=11 / I=24 / R=22 / B=0** |
| Verified rate | **19.0%** |

---

## 1. Per-worker verdict matrix vs B28 / B30 baselines

| W | Set | B28 V/I/R/B | B30 V/I/R/B | **B32 V/I/R/B** | ΔV vs B30 |
|---|---|---|---|---|---|
| 1 | chat_router_smoke | 0/0/7/0 | 0/1/6/0 | **4/0/3/0** | **+4** |
| 2 | stdlib_skills_core | 1/0/8/0 | 1/3/5/0 | 1/5/3/0 | +0 |
| 3 | control_ir_ops | 6/1/2/0 | 2/0/7/0 | 2/1/6/0 | +0 |
| 4 | permissions_and_safety | 3/5/0/0 | 4/3/1/0 | 4/3/1/0 | +0 |
| 5 | multi_agent_and_mcp | 0/3/4/0 | 1/3/3/0 | 0/4/3/0 | -1 |
| 6 | plan_mode + fp_0011 | 0/10/0/1 | 0/9/2/0 | 1/6/4/0 | +1 |
| 7 | long_session_v1 | 2/5/0/0 | 2/5/0/0 | 1/5/1/0 | -1 |
| **Total** | — | 12/24/21/1 | 10/24/24/0 | **11/24/22/0** | **+1** |

Verified-rate trajectory: B27 0/58 → B28 12/58 (20.7%) → B30 10/58 (17.2%) → B32 11/58 (19.0%). The dip in B30 partially recovers in B32; the residual variance is explained by the ablation (= §3).

---

## 2. B30-NEW-1/2/3 e2e verification

### 2.1 ✅ NEW-1 hot_list_n bump (10 → 16)

Primary data across workers:
- **W1**: cold-start tools array contains 17 tools, including `skill__eval` and `skill__index_docs`, across 7/7 first-turn LLM requests
- **W2**: same — 17 tools cold-start, S1 / S7 / S9 see seed entries as direct aliases without `list_actions` discovery first
- **W4**: 17 tools in cold-start array
- **W7**: 51/51 router turns over the long-session set show both `skill__index_docs` and `skill__eval` visible

The bump from 10 → 16 reached every fresh session. No regression in seed visibility detected.

### 2.2 ✅ NEW-2 `skill__eval` seed

- **W2 S7**: LLM directly invoked `invoke_action(action_name="skill__eval", ...)`. The B30 hallucination `skill__direct_llm_eval` did NOT recur.
- **W3 S6**: LLM called `skill__eval` directly (= verified at the routing layer; downstream failure was a separate unsafe-python issue).
- **W5 S1**: LLM invoked `invoke_action(action_name="skill__mcp_search")` — adjacent confirmation that seed-visible discovery beats hallucinated catalog probes.

The discoverability-vs-disambiguation distinction (= B30 §4.2) closed cleanly.

### 2.3 ✅ NEW-3 wipe recipe (reyn/local/)

- **W1**: S3 / S6 isolation clean; no `list_comprehension_generator` carry-over despite earlier scenarios creating local skills.
- **W3**: applied; no contamination observed.

**However**: the wipe recipe is still incomplete (= §4.4).

### 2.4 ⚠️ NEW-3 wipe gap (= 4 worker independent reproduction)

**B32-NEW-FINDING-1 (HIGH)**: `.reyn/state/wal.jsonl` is NOT in the wipe recipe. Pending skill completions from one scenario `session_restored` into the next scenario's first turn. W1 S5 directly observed this (= S4's word_stats completion injected into S5 context).

**B32-NEW-FINDING-2 (HIGH)**: `.reyn/agents/<agent>/history.jsonl` is NOT in the wipe recipe either. Earlier-scenario conversation turns appear as context in later scenarios.

**Independent reproduction**:
- W1 (chat_router_smoke S5)
- W2 (stdlib_skills_core, S1→S2 carryover)
- W5 (multi_agent S1→S4 bleed; worker self-corrected mid-batch by adding the wipe)
- W6 (plan_mode session_restored on every scenario start)

**Fix**: extend `docs/deep-dives/contributing/dogfood-discipline.md` per-scenario wipe recipe to include `wal.jsonl` and `history.jsonl`. Companion task #98 already filed.

---

## 3. W3 regression cluster — ablation resolved

Memory `feedback_iterative_replay_patch_disambiguation.md` was applied for the first time at scale. 8th parallel sonnet ran `scripts/llm_replay.py` with `--patch` against B28 traces for the affected scenarios.

### 3.1 Per-scenario attribution

| Scenario | Mid-batch B30 hypothesis | **Ablation verdict** | Confidence |
|---|---|---|---|
| **S2 file_glob_grep** | "B27-M2 file__grep drop made LLM fall back to file__list" | **CONFIRMED — B27-M2** | HIGH (3/3 vs 3/3) |
| **S4 web_fetch_url** | "B29-MED-3 cwd injection pushed LLM toward plan-first" | **REFUTED — probabilistic N=1 noise** | HIGH |
| **S5 sandboxed_exec_simple** | "B28-Q2 classification shift only" | **CONFIRMED — Q2 classification rule, no code regression** | HIGH |
| S7 recall_indexed_source | "B28-MED-1 seed reshaped LLM RAG mental model" | UNRESOLVED — `plan.py._PLAN_DESCRIPTION` "recall" example is a candidate but unchanged B28→B30 | needs follow-up |
| S8 judge_output_direct | "B29 eval audit shifted judge_phase discrimination" | OUT OF SCOPE — async skill timing, not first-LLM-call routing | — |

### 3.2 Ablation method

`P-M2` for S2: re-inject `file__grep` into the tools array of B28's S2 first-request trace via llm_replay's `--patch` syntax. N=3 calls per condition.

- **Baseline** (no patch, post-M2 tool array): 3/3 LLM choices = `invoke_action(file__list)` (= wrong args, KeyError)
- **P-M2 patch** (file__grep present): 3/3 LLM choices = `invoke_action(file__grep)` (= correct)

The B28 verified outcome was a single lucky run; the underlying attractor (= file__list with wrong args when file__grep is absent) is **persistent**, not regressive.

### 3.3 Discipline lessons

1. **Mid-batch B30 reflex was wrong.** I wrote "B29-MED-3 副作用で plan-first" inline — ablation refuted that hypothesis. The cost of writing inference in the moment is that future readers (= myself in this very batch) anchor on it.
2. **N=1 verified ≠ structural verified.** B28's S2 verified was a probabilistic outcome. Without N≥3 we cannot distinguish "fix works" from "LLM happened to choose right that run."
3. **Ablation is cheap once tooling is in place.** `llm_replay.py --patch` ran the 2-scenario ablation in well under an hour. The marginal cost per future regression is now bounded.

The B30 retrospective said "the dogfood-discipline becomes load-bearing rather than just careful." B32 was the first batch where that load was carried.

---

## 4. New findings surfaced in B32

### 4.1 [HIGH-pre-existing, ablation-confirmed] file__grep absence = persistent file__list mis-call attractor (B27-M2 ablation)

§3.1 S2. Removing `file__grep` from the seed without a routing rule for it created an LLM attractor toward `file__list` with wrong args. This is the **persistent** state of S2, not a B30 regression.

**Fix candidate**: either
(a) implement `file__grep` routing rule + handler (= FP-0034 §D20 follow-up), OR
(b) add an envelope-layer hint when `file__list` is called with non-path args (= "did you mean file__glob? did you mean a search tool?").

(a) is the principled fix. (b) is the cheap fix.

### 4.2 [HIGH-new] S1 file_read_via_chat — `(answered)` injection race vs async skill (W3)

B28 + B30 verified, B32 refuted. The async `read_local_files` skill was still running when the `(answered)` synthetic token was injected into the router context. The router LLM composed a reply without the skill's output — hallucinated generic principles content instead.

This is a **race condition** in the router's quiescence detection, not a fix-induced regression. Independently observable.

**Fix candidate**: router should not inject `(answered)` until all skills spawned this turn have reached terminal state (= `skill_run_completed` / `skill_run_failed` / `skill_run_interrupted`).

### 4.3 [HIGH-new] Async narration hallucination (W6 s-fp12-completion-2)

Router replied with "I will notify you / /tasks" with `finish_reason=stop` and ZERO tool calls. The spawn-ack-style language was emitted without any actual invocation — a hallucination of the FP-0012 spawn-ack pattern.

**Fix candidate**: spawn-ack language template should never be allowed when no tool was invoked. Envelope-layer guard.

### 4.4 [HIGH-pre-existing-confirmed] B23-PRE-1 description ambiguity widens with seed expansion (W7 S1)

B30 verified, B32 refuted on the same scenario. W7's turn-2 LLM message: *"skill__skill_builder, skill__skill_improver, skill__skill_importer, and skill__eval tools all have the same description and input schema"*. The B23-PRE-1 ambiguity (= multiple skill_* descriptions converging) widened when NEW-2 added `skill__eval` to the same description class.

**Fix candidate**: skill description audit — the four skills with shared description shape need disambiguation. B29 eval audit already addressed eval vs skill_improver; this is the next pair (= eval vs builder vs importer, plus the eval vs improver overlap still being in the same class).

### 4.5 [HIGH-pre-existing-confirmed] Double/triple/quad dispatch reproduces (W6)

B30 W6 first observed; B32 W6 reproduces:
- narr-3-skill-builder: 3 invocations
- s-fp11-1: 2 invocations
- s-fp12-spawn-1: 2 invocations

Pattern: router makes a tool call on each turn when the user re-phrases or the agent asks a clarifying question. **Not addressed by any wave to date**. Needs its own issue + investigation.

### 4.6 [HIGH-new-pattern] Args double-serialization (W5 S3)

LLM passed `args` as a JSON-encoded string `"{\"message\":\"...\"}"` instead of an object `{"message":"..."}`. Triggered `invalid_args` validation.

**Fix candidate** (envelope-layer): wrapper accepts double-serialized args by detecting type=string-starting-with-`{` and JSON-parsing once. Defensive normalization at the entry point. Memory `feedback_envelope_layer_fix.md` directly applicable.

### 4.7 [HIGH-pre-existing] `--allow-unsafe-python` not propagated to postprocessor (W2 S1)

`index_docs` skill's `__post__` postprocessor hits `SafeModeViolation` (`glob` import blocked) even when `--allow-unsafe-python` is set. The flag does not flow into the postprocessor step.

**Fix candidate**: trace the flag plumbing in `src/reyn/op_runtime/preprocessor_executor.py` / postprocessor_executor — the unsafe-python decision needs to apply uniformly to pre + post.

### 4.8 [MED] mcp.operation__install has no routing rule (W4 S2)

`mcp.operation__install` is in scenarios' expected events but has no entry in `_OPERATION_RULES`. `skill_run_spawned` never fires. Adjacent to §4.1 (file__grep): scenarios reference qualified names that don't dispatch.

### 4.9 [MED-rubric] mcp_search async skill interrupted by stdin close (W2 S4/S7/S9-T2)

When stdin closes (= dogfood worker pipes a single user turn and exits), in-flight async skills receive `CancelledError`. `skill_run_completed` doesn't fire. This is the same class as issue #52 (B27-H4 root cause) but observed via a different surface.

---

## 5. Severity rollup

### CRITICAL — none

### HIGH — fix candidates for next wave

| ID | Source | Direction |
|---|---|---|
| **§4.1** | B27-M2 ablation | Implement file__grep handler (a) or add file__list arg-hint (b) |
| §4.2 | W3 S1 race | Block `(answered)` until skills terminal |
| §4.3 | W6 spawn-ack hallucination | Envelope guard on spawn-ack language |
| §4.4 | W7 B23-PRE-1 widening | Skill description audit (eval / builder / importer / improver class) |
| §4.5 | W6 double-dispatch (pre-existing) | Investigation issue + per-turn invocation guard |
| §4.6 | W5 args double-serialize | Envelope-layer JSON-string detection |
| §4.7 | W2 unsafe-python postprocessor | Flag plumbing fix |
| B32-NEW-FINDING-1/2 | 4-worker wipe gap | Update wipe recipe (= task #98) |

### Existing trackers (= no new action needed)

- #52 (B27-H4 root cause) — async skill `acompletion never awaited`. §4.9 reproduces under different surface.
- #53 (web enforcement) — W4 S8 again confirmed `WebFetchConfig` deny field missing.
- #54 (qualified-name multi-provider).

---

## 6. Process notes

### What worked

- **Ablation in parallel with retest** — 7 sonnet B32 + 1 ablation sonnet ran concurrently; the ablation result landed alongside the worker reports and let the aggregate phase resolve attribution immediately. This is the right cadence for "regression-cluster present + structural fix wave landed" batches.
- **Mid-batch fix discipline returned to observation-only phrasing**. After the B30 user reminder, the inline reports said "observation: verdict shifted" not "fix X caused it." The hypothesis paragraphs are properly tagged.
- **Worker prompts asking explicit verification angles** continue to surface specific evidence: every "NEW-1 visible: yes" answer in B32 has a tools-array excerpt behind it.

### What needs adjustment

- **The wipe recipe accumulates** (= now wal.jsonl + history.jsonl after NEW-3's reyn/local/ addition). The pattern is: dogfood surfaces a new contamination class every 2-3 batches. The right fix is probably an automated `reyn dogfood wipe <agent>` command rather than continuing to extend the manual checklist. Note for B33.
- **4 workers independently surfacing the wal.jsonl / history.jsonl gap** is a positive sign for the discipline (= independent reproduction) but also signals the prompt template should ship with the corrected wipe. The Q3 hand-edit by W5 mid-batch is the right operator instinct.
- **W6 still skips findings.md prose**. Same as B30. Promotion to "always write prose first" in the prompt template did not propagate to W6. Probably scope-related: W6 ran 11 scenarios across 3 yaml files, the largest by count. Action: split W6's scope across 2 workers in B33.

### What surprised us

- **B28 W3 verified was lucky N=1**. The ablation evidence (= 3/3 baseline file__list, 3/3 patched file__grep) means the "regression" framing was incorrect: the persistent attractor was always present, just hidden behind a single probabilistic outcome. The implication: any verified count without N≥3 (or ideally N=5 per scenario at this point in the project) is **calibration-grade**, not **shippable-grade**.
- **The wipe recipe is leakier than it looks**. Three independent contamination surfaces (= reyn/local/, wal.jsonl, history.jsonl) all found within 3 batches. The OS architecture's stateful surfaces are richer than the operator's mental model. The `reyn dogfood wipe` command idea (= §6) addresses this gap structurally.

---

## 7. Next batch ready-list (B33 candidates)

In priority order:

1. **Wipe recipe extension** (= B32-NEW-FINDING-1/2 → task #98): add wal.jsonl + history.jsonl. Doc update + dispatch prompt template.
2. **§4.1 file__grep**: pick (a) implement handler or (b) envelope hint. (a) is principled, (b) is cheap.
3. **§4.4 skill description audit** (= eval / skill_builder / skill_improver / skill_importer): description disambiguation, modeled on B29 eval audit.
4. **§4.6 args double-serialize**: envelope-layer JSON-string detection. Defensive normalization.
5. **§4.2 race condition**: router `(answered)` injection should wait for spawned-this-turn skills to reach terminal state.
6. **§4.5 double-dispatch investigation**: separate issue, larger scope.
7. **task #93 verifier integration** (= still gapped, blocks `reyn dogfood run` e2e).
8. **B27-H4 root cause** (= #52) — async skill never-awaited surface reproduces in B32 §4.9.

---

## 8. Cross-reference

- Worker artefacts: `workers/findings-worker-{1,2,4,5,6}.md` + `workers/results-worker-{1..7}.json` (= W3 / W7 wrote results.json only)
- W3 ablation: `workers/w3-ablation-report.md`
- Memory: `feedback_iterative_replay_patch_disambiguation.md`,
  `feedback_pre_conclusion_observation_checklist.md`,
  `feedback_minimize_speculation.md`,
  `feedback_envelope_layer_fix.md`
- Wave 1 + B29 + B30-NEW commits: `c0d5ea8` / `ef0a07f` / `bceee51` / `e17f6df` / `a8e7d34` / `32b28a0` / `1636584` / `1a5be83` / `d87a178` / `c10beb7` / `14c6b6b` / `31f14d8` / `850b81b` / `67e21e3` / `c8fae2e`
- Issues #52 / #53 / #54 still open
