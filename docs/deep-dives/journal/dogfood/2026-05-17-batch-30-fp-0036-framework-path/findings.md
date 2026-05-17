# Batch 30 — Findings (B29 fix-wave verification + framework path probe)

> Third retest of the FP-0036 starter set. Same 58 scenarios, 7 sonnet
> workers, main HEAD `4be42fe` (= post-B29 wave: live_runner + eval audit
> + plan-step cwd + Q2 synthetic event + MED-1 seed + NEW-2 reyn.yaml).
>
> Headline (= observation): V 12 → 10 (= -2 net), R 21 → 24 (= +3),
> B 1 → 0. Q2 synthetic event verified across all workers; one cluster
> (W3 control_ir) lost 4 verified versus B28. Causal attribution between
> B27/B28/B29 fixes and the W3 regression is a hypothesis pending
> ablation, NOT a verified causal finding.

---

## 0. Run summary

| Item | Value |
|---|---|
| HEAD | `4be42fe` (= post-Wave-1 + B29 wave) |
| Tests (pre-batch) | 3336 passed / 5 skipped / 2 xfailed |
| Total scenarios | 58 |
| Workers | 7 sonnet parallel, per-cwd + per-reyn-agent isolation |
| Worktrees | `/tmp/reyn-worktrees/b30-{1..7}` |
| Wall-clock | ~30 min (= longest W5 at 27 min) |
| LLM model | `gemini-2.5-flash-lite` via local LiteLLM proxy |
| Driver | legacy `reyn chat --cui` stdin pipe (= framework path probed via smoke, verifier integration deferred) |
| **Aggregate verdict** | **V=10 / I=24 / R=24 / B=0** |
| Verified rate | **17.2%** (= B28: 20.7%) |

---

## 1. Per-worker verdict matrix vs B27 / B28 baselines

| W | Set | B27 V/I/R/B | B28 V/I/R/B | **B30 V/I/R/B** | ΔV vs B28 | Notes |
|---|---|---|---|---|---|---|
| 1 | chat_router_smoke | 0/0/3/4 | 0/0/7/0 | 0/1/6/0 | +0 | 1 R→I (= S4 word_stats_demo improved) |
| 2 | stdlib_skills_core | 0/2/6/1 | 1/0/8/0 | 1/3/5/0 | +0 | 3 R→I from python.safe + eval audit |
| 3 | control_ir_ops | 0/3/3/3 | **6/1/2/0** | **2/0/7/0** | **-4** | **Regression cluster** (= hypothesis-pending) |
| 4 | permissions_and_safety | 0/7/1/0 | 3/5/0/0 | 4/3/1/0 | +1 | Q2 fix verified 3/4 scenarios |
| 5 | multi_agent_and_mcp | 0/1/6/0 | 0/3/4/0 | 1/3/3/0 | +1 | Q2 fix verified 2/2 scenarios |
| 6 | plan_mode + fp_0011 | 0/6/0/5 | 0/10/0/1 | 0/9/2/0 | +0 | MED-3 partial; plan_emitted 1/3 (= B28 was 2/3) |
| 7 | long_session_v1 | 0/0/7/0 | 2/5/0/0 | 2/5/0/0 | 0 | C1 verified 52/52 turns; Q2 emit 32/32 correct |
| **Total** | — | 0/19/26/13 | **12/24/21/1** | **10/24/24/0** | **-2** | |

---

## 2. Wave 1 + B29 fix verification (= primary observations)

### 2.1 ✅ C1 hot-list filter

Direct trace inspection across all 7 workers:
- W1: 14 tools × 7 scenarios, 0 duplicates
- W2: 14 tools × 9 scenarios, stable
- W3: 14 tools × 9 scenarios, stable
- W4: clean across all
- W5: 14 tools, 0 errors
- W6: no duplicate plan declarations
- **W7: 52/52 LLM request turns, 0 duplicates** (= multi-turn high-N evidence)

This fix is now triply-verified across three batches (B28 / B30 × 2). No regression observed.

### 2.2 ✅ B29-Q2 chat_turn_completed_inline + must_emit_any semantics

Synthetic event behaviour (= primary data):
- **W7**: 32/32 inline conversational turns emit `chat_turn_completed_inline`, 5/5 tool-routing turns emit `routing_decided` instead. Mutual exclusivity holds 37/37.
- **W4**: Event fires on S4/S5/S6/S7 (= the 4 scenarios updated to `must_emit_any` for OR semantics). 3/4 verified; S5 inconclusive due to *rubric* (= reply mentions capability limit rather than budget limit), not event mechanism.
- **W5**: 2/2 target scenarios (S2, S5) emit the event correctly.
- **W1**: 4/7 scenarios emit the event; S5 `must_emit_any` passes but reply rubric still fails.
- **W6 / W2**: Event emits correctly in applicable inline-reply paths.

Event mechanism is solid. The remaining non-verified outcomes that use this event are now **rubric-bound**, not event-bound — exactly the distinction the Q2 design aimed to surface.

### 2.3 ✅ B28-NEW-2 (reyn.yaml python.safe rename) — independent confirmation

**W2**: `preprocessor_step_completed` without `permission_denied` for word_stats_demo (S5 / S6). Fix landed mid-B28, B30 confirms across independent worker.

### 2.4 ⚠️ B28-MED-1 (skill__index_docs seed) — partial / root cause identified

**W2 primary observation**: `DEFAULT_HOT_LIST_SEED` now has 12 items but `hot_list_n=10` (= `ActionRetrievalConfig` default in `src/reyn/config.py`) caps `get_top_n()` output. Items at indices 11 (`skill__read_local_files`) and 12 (`skill__index_docs`) are **truncated** on cold-start sessions. The LLM never sees `skill__index_docs` as a direct alias and falls back to hallucinated `rag.operation__create_index` / `rag.operation__add_source`.

**W1 counter-observation**: S3 in chat_router_smoke listed `skill__index_docs` correctly via `list_actions` enumeration (= different surface than hot-list aliases). So the **discoverability via list_actions is intact**; only the direct-alias path is truncated.

**Fix candidate**: bump `hot_list_n` default to ≥14 in `src/reyn/config.py` so the full seed reaches the LLM as direct aliases on cold start.

### 2.5 ⚠️ B29-MED-3 (PLAN-STEP-PATH cwd injection) — partial

**W6 primary observation**: Every plan step system prompt now contains `"You are at project root: /private/tmp/reyn-worktrees/b30-6"`. The injection is present. **However** step LLMs still call `reyn_src_read({"path": "principles.md"})` — bare filename. Tool response: `"The file 'principles.md' does not exist in the Reyn repository."`

The cwd anchor is **necessary but not sufficient**. The plan goal + step description LLMs need to carry full paths, not just the cwd line. This is a *plan goal generation* issue, not a *plan step execution* one.

**Fix candidate (= scope outside this wave)**: enrich the plan tool description's example to show full-path step descriptions, OR have the planner expand bare filenames in step descriptions before dispatch.

### 2.6 ⚠️ B29 eval / skill_improver disambiguation — partial

**W2 primary observation**: B28's specific failure (= LLM invoking `skill__skill_improver` for an eval scenario) is gone. However, a **new variant** surfaced: S7 LLM now hallucinates `skill__direct_llm_eval` — a non-existent skill name. Root cause shares §2.4: `skill__eval` is not in the seed at all, so the LLM has no direct-alias hint and the description disambiguation alone is insufficient when the discoverability layer leaves the skill invisible on cold start.

**Fix candidate**: seed `skill__eval` (= 1-line) **and** bump `hot_list_n` per §2.4.

---

## 3. W3 regression cluster (= -4V observation, hypothesis-pending)

**Primary observation** (= directly inspected per-scenario):

| Scenario | B28 verdict | B30 verdict | Primary observation of difference |
|---|---|---|---|
| S2 (file_glob_grep) | verified | refuted | LLM called `file__list` with glob-style args `{match, filter}` → `KeyError:'path'`. B28 had used `file__glob` successfully. |
| S4 (web_fetch_url) | verified | refuted | LLM chose `plan` tool instead of `invoke_action(web__fetch)` directly. `plan` is non-catalog, so `routing_decided` never fires. |
| S7 (recall_indexed_source) | blocked | refuted | LLM replied inline: "recall is only available in plan steps". `chat_turn_completed_inline` fires; reply rubric does not match. |
| S8 (judge_output_direct) | blocked | refuted | `judge_phase` dispatched asynchronously; single-turn reply is "I will notify you," not the synchronous phase JSON. Events verified (routing_decided / skill_run_spawned), reply fails. |
| S5 (sandboxed_exec_simple) | inconclusive | refuted | Environment unchanged (= no sandbox backend), but classification shifted: `routing_decided` now fires with `exec__run/outcome:success` though no `sandboxed_exec_*` events follow. |

**What is verified**: each of the above 5 scenarios changed verdict between B28 and B30 in the direction shown.

**What is hypothesis (= NOT verified)**:
- "B28-MED-3 cwd injection pushed the LLM toward plan-first strategy" (= S4)
- "B28-MED-1 seed changed the LLM's understanding of recall" (= S7)
- "B27-M2 file__grep seed drop caused the LLM to fall back to file__list with wrong args" (= S2)

Each hypothesis is plausible from the change set landed between B28 and B30. None has primary-data support: the LLM's reasoning is not directly observable, and each B27/B28/B29 fix landed simultaneously in the merge wave so their effects are confounded.

**Resolution path** (= per memory `feedback_iterative_replay_patch_disambiguation.md`):
- Capture the B28 traces that produced the verified verdicts for S2/S4/S7/S8/S5.
- Run `scripts/llm_replay.py` with `--patch` to undo each suspected change one at a time (= MED-3 cwd line / MED-1 seed addition / M2 grep drop) and observe whether the regression scenario reverts to its B28 behaviour.
- The patch whose removal restores verified verdict is the causal factor.
- N ≥ 5 per patch — N=1 in a single batch is hypothesis only.

Until that ablation runs, the W3 regression is **observed but not causally attributed**.

---

## 4. New findings surfaced in B30

### 4.1 [HIGH] hot_list_n cap truncates expanded seed (W2)

`hot_list_n = 10` in `ActionRetrievalConfig` default vs `DEFAULT_HOT_LIST_SEED` length 12. Truncation is silent. Triggered the MED-1 hallucination not fixing as expected on cold start.

**Fix**: bump `hot_list_n` default to ≥14 (= seed length + headroom). Add an invariant test asserting `hot_list_n >= len(DEFAULT_HOT_LIST_SEED)` on default config so future seed expansion can't re-introduce the same issue.

### 4.2 [HIGH] skill__eval missing from seed (W2)

§2.6: new hallucination variant `skill__direct_llm_eval` because `skill__eval` is not surfaced as a hot-list alias. Companion to §4.1.

**Fix**: add `"skill__eval"` to `DEFAULT_HOT_LIST_SEED` once §4.1 cap is bumped.

### 4.3 [HIGH] reyn/local/ contamination between scenarios (W1)

`skill_builder` writes persistent skill files to `reyn/local/<name>/`. Subsequent scenarios see those skills in `list_actions` enumeration. Observed: S6's `list_comprehension_generator` skill contaminated S3's skill list.

**Fix**: dogfood worker wipe recipe must include `reyn/local/`. Update worker prompt template + the dogfood runner equivalent in `src/reyn/dogfood/runner.py` once verifier integration lands.

### 4.4 [HIGH-pre-existing, newly reproducible] Double/triple/quad dispatch (W6)

Router spawns 2–4 `invoke_action(skill_builder)` calls per user request across sequential routing turns. Observed across 4 separate scenarios in W6. This is a pre-existing bug (= present in earlier batches per memory) that B30 reproduces clearly.

**Resolution path**: separate issue + dedicated investigation. Not in this wave.

### 4.5 [HIGH-already known via #53] WebFetchConfig has no deny field (W4)

`WebFetchConfig` (`src/reyn/config.py:662-684`) declares only `verify_ssl` and `ca_bundle`. The `web.fetch: deny: true` config key passed through `_build_web_fetch_config()` is silently discarded. Issue #53 (= web enforcement bug) now has its root cause pinpointed.

**Note**: not a B30 fix candidate — owned by #53.

### 4.6 [LOW] search_actions: unknown_tool attractor persists (W5 S2, W7 S3)

LLM calls `search_actions(query=...)` for natural-language tool discovery. Returns `unknown_tool` because `search_actions_visible` requires `action_retrieval.embedding_class` configured. The attractor pattern reproduces on phrasing like "github MCP server recent PRs".

**Note**: scenario environment limitation; fix scope depends on whether dogfood is to validate the `search_actions` path (= configure embedding) or to harden the LLM against the unknown_tool path (= envelope-layer hint per `feedback_envelope_layer_fix.md`).

### 4.7 [HIGH-LLM-compliance] file__write KeyError 'content' (W4 S1)

LLM sends `{path: ..., text: ...}` while handler reads `args["content"]`. The schema explicitly requires `content` — this is LLM schema non-compliance.

**Fix candidate**: envelope-layer — handler returns a clear "missing required field: content" error rather than raising `KeyError`. Already noted in B28 findings (§3.3); reproduces in B30.

---

## 5. Calibration observation (= no formal Brier yet)

| Band | B27 | B28 | B30 |
|---|---|---|---|
| Verified | 0 | 12 | **10** |
| Inconclusive | 19 | 24 | 24 |
| Refuted | 26 | 21 | 24 |
| Blocked | 13 | 1 | 0 |

The trajectory is **not monotone**. B27→B28 was strict improvement; B28→B30 lost 2 verified. The interpretation depends on whether the W3 regression is causally tied to the B29 wave (= net unintended consequence) or independent noise (= LLM probabilistic routing). Until §3's ablation runs, both interpretations are equally consistent with the data.

**What we DO know empirically**:
- C1 is rock-solid (= 3 batches, all clean)
- Q2 event mechanism works as designed
- Discoverability gaps (= §4.1, §4.2, §4.3) are concrete structural bugs

**What we do NOT know yet**:
- Whether B29 wave was net-positive on rubric-pass rate at the LLM level
- Whether W3 regression points at a real subsystem coupling or LLM noise

---

## 6. Severity classification

### CRITICAL — none

### HIGH — fix candidates for next wave

| ID | Source | Fix sketch |
|---|---|---|
| B30-NEW-1 | §4.1 | Bump `hot_list_n` default to ≥14 + invariant test |
| B30-NEW-2 | §4.2 | Seed `skill__eval` (= after NEW-1) |
| B30-NEW-3 | §4.3 | Worker wipe recipe includes `reyn/local/` |
| W3 regression triage | §3 | Run llm_replay --patch ablation on B28 traces to attribute cause |

### Pre-existing (= separate tracks)

| ID | Status |
|---|---|
| Double/quad dispatch (§4.4) | Separate issue needed |
| #53 root cause (§4.5) | Already filed; pinpointed in this batch |
| search_actions attractor (§4.6) | Environment / LLM hardening decision pending |
| file__write KeyError (§4.7) | Envelope-layer fix candidate |

---

## 7. Process notes

### What worked

- **B30 verification angles** per worker (= C1 / Q2 / MED-1 / NEW-2 / MED-3 / etc.) put structural fix evidence inline with each scenario's verdict. Made the regression cluster easy to spot vs B28.
- **W5's long-running duration** (= 27 min vs ~14 min avg) prompted a user gut-check that surfaced the worker-stuck risk early. We learned from B27's worker-E experience and considered killing it; the worker eventually completed on its own. Future: tighten the "long-tail vs stuck" heuristic — observation is still on the side of "wait if no error", but a 2× cap on average might be the right kill threshold.

### What needs adjustment

- **Causal attribution discipline**: when a verdict regresses, the impulse to write "fix X caused this" is strong. Two paragraphs of inference language slipped into my mid-batch summary; corrected after a user reminder to "remember the dogfood principle." Action: enforce observation-first phrasing in worker prompts + main-agent aggregation. Specifically, write "verdict changed; hypothesis: ...; ablation to verify" rather than "fix X caused regression."
- **`reyn dogfood run` framework path was probed (= smoke) but not used for the batch**. Live_runner emits replies but the verifier triad output is `detail: {}` — verifier integration is the remaining gap. Tracked as task #93.
- **W6 finding inconclusive rate (9/11)** is high. plan-mode + fp_0011 scenarios have ambiguous expected fields; partial-expected scenarios default to inconclusive per discipline. Either tighten those scenarios' rubrics or accept inconclusive as the floor for that set.

---

## 8. Next batch ready-list (= B31 candidates)

In priority order:

1. **B30-NEW-1 + NEW-2** (= hot_list_n bump + skill__eval seed). 5-line patch. Same fix-wave pattern. Will exercise the W2 MED-1 finding.
2. **W3 regression ablation** (= llm_replay --patch on B28 vs B30 traces). Information-gathering, no source change. Could run in parallel with NEW-1/2.
3. **B30-NEW-3 worker wipe recipe** (= update dogfood worker prompt template + runner if applicable). 2-line patch.
4. **`reyn dogfood run` verifier integration** (= task #93). Medium-scope, unlocks the framework path for B32+.
5. **Double-dispatch investigation** (= §4.4). Separate issue.
6. **calibration recalibration** (= once 4 is done, scenarios can be re-rated against the framework's runner).

---

## 9. Cross-reference

- Worker artefacts: `workers/findings-worker-{2,3,5,6,7}.md` + `workers/results-worker-{1..7}.json` (= W1 / W4 wrote results.json only; prose recoverable from this aggregate)
- B27 / B28 baselines: prior journals under `docs/deep-dives/journal/dogfood/`
- Wave 1 fixes (B27): commits `c0d5ea8` / `ef0a07f` / `bceee51` / `e17f6df` / `a8e7d34` / `32b28a0` / `1636584`
- B28-NEW-2: `1a5be83`
- B28-MED-1 (seed `skill__index_docs`): `d87a178`
- B29 wave: `c10beb7` (live_runner) / `14c6b6b` (eval audit) / `31f14d8` (plan-step-path) / `850b81b` (Q2 synthetic event) / `c89010f` (replay fixtures + xfail re-org)
- Open follow-up issues: #52 (B27-H4 root cause), #53 (web enforcement — root cause pinpointed §4.5), #54 (qualified-name multi-provider)
- Memory: `feedback_pre_conclusion_observation_checklist.md`, `feedback_minimize_speculation.md`, `feedback_iterative_replay_patch_disambiguation.md`
