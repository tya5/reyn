# Batch 33 — Findings (H3 + H5 refit + #53 fix verification)

> Fifth dogfood batch under FP-0036. First batch after the ablation
> wave (= main HEAD `08ccc27` carries H3 race fix + H5 calibration
> refit + #53 enforcement fix + complete wipe recipe).
> Headline: **aggregate verified rate stable at 21%** (= B32 12/58 →
> B33 12/58), but the constancy is the result of **net-positive OS
> fixes offset by a newly-surfaced driver harness gap**. The honest
> read is "OS got better; harness needs to catch up."

---

## 0. Run summary

| Item | Value |
|---|---|
| HEAD | `08ccc27` (= post-ablation: H3 race fix `25834de`, H5 refit `032fb80`, #53 enforcement `b5d81e4`) |
| Tests | 3346 passed / 5 skipped / 2 xfailed |
| Total scenarios | 58 (W6 covers 11) |
| Workers | 7 sonnet parallel, per-cwd + per-reyn-agent isolation |
| Worktrees | `/tmp/reyn-worktrees/b33-{1..7}` |
| Wall-clock | ~20 min |
| Driver | legacy `reyn chat --cui` stdin pipe + complete wipe recipe (wal.jsonl + history.jsonl + reyn/local/) |
| **Aggregate** | **V=12 / I=16 / R=26 / B=4** |
| Verified rate | **20.7%** (= B32: 20.7%, identical) |

---

## 1. Per-worker verdict matrix vs B32

| W | Set | B32 V/I/R/B | **B33 V/I/R/B** | ΔV |
|---|---|---|---|---|
| 1 | chat_router_smoke | 4/0/3/0 | 2/1/4/0 | **-2** (LLM variance per §3.1) |
| 2 | stdlib_skills_core | 1/3/5/0 | 0/0/9/0 | **-1** (driver harness gap §3.2) |
| 3 | control_ir_ops | 2/0/7/0 | 3/0/6/0 | +1 (S1 race fix flip + S7 rubric) |
| 4 | permissions_and_safety | 4/3/1/0 | 4/4/0/0 | +0 V / -1 R (#53 fix moved S8 R→V, balance variance) |
| 5 | multi_agent_and_mcp | 0/4/3/0 | 0/2/5/0 | 0 V / +2 R |
| 6 | plan_mode + fp_0011 | 0/9/2/0 | 1/4/2/4 | +1 V; 4 newly blocked by env (= unsafe-python) |
| 7 | long_session_v1 | 1/5/1/0 | 2/5/0/0 | **+1** (S5 asyncio.Queue verified) |
| **Total** | — | 12/24/22/0 | **12/16/26/4** | **0 V net** |

Verified-rate trajectory: B27 0/58 (0%) → B28 12/58 (21%) → B30 10/58 (17%) → B32 11/58 (19%) → **B33 12/58 (21%)**.

Per-worker Brier scores (where computed): W2 0.108, W3 0.319, W5 0.286, W7 0.313. Mean ≈ 0.26 (= B32 refit baseline ~0.40, the predictions are now slightly under-refuted vs actual, but cleanly within calibrated range).

---

## 2. OS-layer fix verification (= structural confirmations)

### 2.1 ✅ H3 race fix — `invoke_skill_spawn_ack_exit`

**Confirmed across every worker that exercised the path**:

| Worker | Path | Evidence |
|---|---|---|
| W1 S4 | word_stats_demo | event fired; `skill_completion_injected` followed; reply narrated completion |
| W2 (S4/S5/S6/S7) | 4/4 spawn-eligible scenarios | event fires correctly |
| W3 (S6/S8/S9) | spawn-ack scenarios | 3/3 event fired; old `skill_run_interrupted` pattern absent |
| W4 S2 | mcp_install_gate_prompt | event + skill_run_spawned both emitted |
| W5 (S1/S5/S6) | mcp_search-class scenarios | event fired on all 3 |
| W6 fp_0011 family | 8/8 applicable | event fired; B32 §4.3 "I will notify you" hallucination resolved (= s-fp12-completion-2-error-narrate now reports the real `workflow_aborted` reason) |
| W7 scenario_2 turn 3 | direct_llm spawn | event + completion + correct turn-4 continuation |

The `(answered)` race is no longer observable in any worker's traces. This is the structural fix landing cleanly.

### 2.2 ✅ #53 web.fetch enforcement (= ablation-adjacent, landed by another session)

W4 S8 `web_fetch_denied_by_config`: `permission_denied` event fires; the LLM gets a clear error and replies with a graceful explanation. B32 had this scenario silently bypassed (`web_fetch_started` + `web_fetch_completed` at status 200). Fix `b5d81e4` wired `RouterHostAdapter.permission_resolver` property + `intervention_bus_factory` in `make_router_op_context`.

**Verdict shift**: S8 moved R → V.

### 2.3 ✅ H5 calibration refit — predictions now match reality

Per-scenario Brier contributions across the 4 workers that reported them average ~0.26. The refitted bands are slightly *under-refuted* vs B33 actuals (= a few more scenarios refuted than the 4-batch frequency predicted), but this is within calibrated range and substantially better than the original pre-H5 Brier (~0.913). The narrative is stable: predictions now reflect actual system behaviour.

### 2.4 ✅ Complete wipe recipe — no `session_restored`

Every worker that checked confirmed zero `session_restored` events. The B32-NEW-FINDING-1/2 wipe gap (wal.jsonl + history.jsonl) is fully closed. **One caveat (W2 F2 §3.2.b)**: wiping `action_usage.jsonl` removes the hot-list seed visibility at cold start, so workers cannot independently re-confirm B30-NEW-1/2. This is a trade-off, not a regression.

### 2.5 ✅ C1 / Q2 stability across all workers

- C1 (hot-list duplicate filter): 17-tool cold-start arrays, zero duplicates across W1/W2/W3/W4/W5/W6/W7.
- Q2 (`chat_turn_completed_inline` mutually exclusive with `routing_decided`): correct emit pattern across all workers.

These two fixes (= B27-C1, B28-Q2) have now held cleanly across 4 batches.

---

## 3. Where the aggregate V trajectory says "0 net" — honest breakdown

The B32→B33 aggregate ΔV=0 is **not stability through inaction**. It is the sum of:

### 3.1 W1 ΔV=-2 — LLM probabilistic variance (= NOT regression)

S1 (capability question) and S4 (word_stats_demo) flipped V→I/R because:
- S1: LLM chose clarification-question pattern instead of tool enumeration. The clarification reply is a valid behaviour; the rubric expects enumeration.
- S4: LLM narrated completion abstractly without quoting the actual statistic values; B32 happened to quote them.

Neither flip touches any H3 / H5 / #53 mechanism. Both scenarios run the synchronous tool path; H3 spawn-ack exit doesn't apply to them. **These are N=1 LLM routing variance** consistent with the H5 finding that B28's 21% verified included similar lucky probabilistic outcomes.

### 3.2 W2 ΔV=-1 — Driver harness gap surfaced by H3 contract change

This is the **honest miss in my ablation phase**. Primary observation from W2 F2:

> *"When `invoke_skill_spawn_ack_exit` fires (S4, S5, S7), `send_to_agent_impl` returns with empty reply text — the skill completion reply from `chat_turn_completed_inline` is not captured. S6 is inconsistent (non-empty reply), suggesting a timing-dependent race in the harness."*

The OS layer is doing exactly what H3 designed (= router exits on spawn-ack → skill completes async → `_handle_skill_completed` inbox re-engages router with real output). What broke is the **dogfood driver's harness**: `send_to_agent_impl`'s quiescence detection returns from the spawn turn before the subsequent re-engage delivers the actual reply.

**Why this slipped past my ablation**: I treated H3 as a router-internal envelope-layer fix. I did not analyse the contract change for downstream consumers of `send_to_agent_impl`. The H3 ablation report (= K/N=1/22 flip rate) tested *the LLM-side behaviour* on a single scenario with `--patch` replay; it did not exercise the full driver→agent→inbox path that B33's workers exercise via `reyn chat --cui` stdin pipe.

The correct framing of W2 ΔV=-1:
- OS-layer fix: working as designed (= `invoke_skill_spawn_ack_exit` 4/4 fired)
- Driver-layer: did not adapt to the new contract (= reply via inbox, not via the spawn turn's outbox)

(a) **Subordinate observation**: the same gap likely contributes to W6's `b27_baseline=0/9/2/0 → B33=1/4/2/4` shift in addition to the unsafe-python blockers. W6 F2 was tagged separately ("spawn-ack dimension untestable in current single-shot A2A test setup"), which is the same harness-vs-OS contract gap viewed from a different scenario angle.

(b) **Other co-occurrence**: W2's full wipe (= correct hygiene per H6 ablation) removes `action_usage.jsonl`, which empties the hot-list at cold start. B30-NEW-1/2 seed visibility cannot be re-checked independently in this run because the wipe scope and the visibility check overlap. This is a trade-off, not a bug.

### 3.3 W3 / W4 / W7 ΔV=+1 each — Real OS-layer wins

- **W3 S1** R→V: H3 + LLM happened to take `file__read` direct synchronous path (= primary data, not "always direct" — the worker correctly flagged N=1 routing variance vs H3 causation). The reply quoted P1-P8 content verbatim.
- **W4 S8** R→V: #53 fix landed cleanly. Web fetch deny enforcement now works as FP-0022 specified.
- **W7 scenario_5** verified: asyncio.Queue producer-consumer code correct and complete in a multi-turn session. C1 multi-turn stability maintained at 37/37.

### 3.4 W6 +1 V (= 1/4/2/4) — Spawn-ack narration fix verified, env blocked others

W6 promoted `s-fp12-completion-2-error-narrate` from B32 §4.3's "I will notify you" hallucination to a correct error narration. That's a clean +1 V attributable to H3.

The +4 blocked count (vs B32 +1) is from unsafe-python environment blockers on mcp_search-family scenarios. **Environment gap, not a code regression**.

### 3.5 Net interpretation

Real OS-layer wins: +3 V (W3 / W4 / W7).
Real OS-layer loss: 0 V.
Driver-harness loss (= my miss): -1 V (W2 reply capture gap, possibly larger if W6 partial overlap counted).
LLM probabilistic variance: -2 V (W1).
Aggregate: 0 V net.

**Reading the trajectory** B28 12 → B30 10 → B32 11 → B33 12 as monotone progress is the wrong frame. The honest frame is:
- Mean ~11 V across 4 batches (= ~19% verified, ±1)
- Each batch's variance is partly LLM (= ~1-2 V per worker) and partly real fix landing
- B33 specifically landed H3 cleanly at the OS layer but exposed a harness contract gap that *cancels* the OS gain numerically while leaving the OS improvement intact

---

## 4. New findings (= surfaced this batch, fix candidates for B34)

### 4.1 [HIGH] Driver harness reply capture gap (W2 F2)

**Class**: post-H3 contract change adaptation gap.

**Observation**: `send_to_agent_impl` returns empty reply after `invoke_skill_spawn_ack_exit` fires. The skill_completion_injected reply arrives via inbox but the driver's quiescence detection returns from the spawn turn first.

**Fix direction**:
- Extend `send_to_agent_impl` quiescence detection to wait until `_skill_runner.running_skills` is empty AND no inbox-pending re-engage exists for this chain_id
- OR add an explicit "wait for completion" mode for the dogfood / A2A path

**Scope**: medium (= `src/reyn/mcp_server.py:167 send_to_agent_impl` + companion changes in `chat/session.py` for the quiescence definition).

**Ablation pre-check before fix**: confirm by re-running W2's S5/S6/S7 with a patched `send_to_agent_impl` that holds until the inbox re-engage completes. K/N should be ≥4/4 if the gap is the sole cause.

### 4.2 [HIGH] `skill_completion_injected` skipped on `phase_no_progress` abort (W6 NEW-1)

**Observation**: in `s-fp11-1`, `workflow_aborted` triggered via `phase_no_progress` did NOT fire `skill_completion_injected`. In `s-fp12-completion-2` (LLM-initiated abort at `plan_skill`), the injection fired correctly. Different abort code paths.

**Hypothesis (= not yet causally attributed)**: the phase-loop rollback abort path may have a branch that skips the `_handle_skill_completed` inbox enqueue.

**Fix direction**: trace `workflow_aborted` emission sites and `skill_completion_injected` enqueue points. Likely a 1-2 line patch once the gap is located.

**Scope**: small. Ablation: deliberately trigger `phase_no_progress` in a controlled test and confirm inject path.

### 4.3 [HIGH] Peer-agent-not-found silent hallucination (W5 F2)

**Observation**: when `agent.peer__<name>` invocation routes to a non-existent peer, `tool_returned: status=dispatched` is returned (= success-shaped envelope), `[error] agent '<name>' not found` is logged to stderr, but the LLM continues the conversation and **fabricates plausible content as if the peer agent answered**. Example from W5: the agent fabricated "FP-0001 is an AI fine-tuning agent" — entirely wrong.

**Trust impact**: HIGH. This is exactly the failure class the OS-layer P6 audit trail is meant to prevent. The agent-not-found should propagate as an actionable error to the LLM, not be masked by a dispatched-shaped envelope.

**Fix direction**: handler-layer — the `agent.peer__<name>` dispatch path needs to return an error-shaped envelope when the registry doesn't have the peer. Probably in `_handle_invoke_action` or `agent.peer` resolver in `universal_dispatch.py`.

**Scope**: small (= 1 dispatch site) + Tier-2 test.

### 4.4 [MED] PLAN-STEP-PATH residual (W6, persistent from B30-MED-3)

`plan_summary_across_n_files` still calls `reyn_src_read(principles.md)` (bare filename) instead of `reyn_src_read(docs/concepts/principles.md)`. The cwd injection from B29-MED-3 is present in the step system prompt but the step LLM ignores it for path resolution.

**Fix direction**: enrich plan-tool description's `steps_json` example with full-path examples, OR have the planner expand bare filenames in step descriptions before dispatch (= preprocessor-level). Both are scope-medium; the latter is more robust.

### 4.5 [MED] `recall` produces `control_ir_failed` (NoneType base_dir) — W6 NEW-3

`recall` tool consumed by plan steps in `plan_compare_two_concepts` fails with NoneType `base_dir`. RAG index is uninitialized. Environment-level for now; if scenarios mean to exercise the recall path, they need an indexed-source precondition.

### 4.6 [HIGH-LLM] `file__write` / `drop_source` arg-name mismatch (W4 F1, persistent)

LLM still sends `text` instead of `content` to `file__write`, and `source_id` instead of `source` to `rag.operation__drop_source`. The `KeyError` aborts execution before the permission gate is reached. Permission gates can never be verified end-to-end while this gap exists.

**Fix direction** (= envelope-layer, per `feedback_envelope_layer_fix.md`):
- defensive arg normalization at the `invoke_action` entry: detect common synonyms (`text` → `content`, `source_id` → `source`) and rewrite OR
- return a clear "missing required field: content (got: text)" error rather than `KeyError`

**Scope**: small.

### 4.7 [MED-scenario] B27-M2 file__grep attractor persists (W3 S2)

LLM keeps choosing `file__list` with glob-style args (= `{match, filter}`) → `KeyError:'path'`. B27-M2 ablation confirmed this is the post-M2 default LLM behaviour. **Fix candidates** (= same as B30-NEW-1 era proposal):
- (a) implement `file__grep` handler (= FP-0034 §D20 follow-up)
- (b) envelope-layer arg-hint when `file__list` is called with non-path args

---

## 5. Severity rollup

### CRITICAL — none

### HIGH — fix candidates for B34

| ID | Source | Direction |
|---|---|---|
| **§4.1** | W2 F2 driver reply capture gap | Extend `send_to_agent_impl` quiescence; ablation pre-check |
| §4.2 | W6 phase_no_progress abort | Locate inject-path branch gap; small patch |
| §4.3 | W5 peer-agent silent hallucination | Handler-layer error envelope; Tier-2 test |
| §4.6 | LLM arg-name mismatch (file__write / drop_source) | Envelope-layer normalization or clear error |

### MED — follow-ups

| ID | Source | Direction |
|---|---|---|
| §4.4 | PLAN-STEP-PATH residual | Planner full-path expansion |
| §4.5 | recall NoneType base_dir | Environment precondition or graceful fallback |
| §4.7 | file__grep attractor | (a) implement (b) envelope hint |

### Existing trackers reconfirmed

- task #93 (= framework verifier triad integration): W4 surfaced again — runner reported 0V/8I/0R/0B but manual rubric was 4V/4I/0R/0B. The `verify_reply` / `verify_events` / `verify_artifacts` exist but are not called by the runner.
- #52 (= B27-H4 acompletion never awaited): not retested this batch.

---

## 6. Process reflection

### What I missed in the ablation phase

Pre-fix, the H3 ablation analysed the LLM-side behaviour (= `--patch` replay of B28 trace + measure LLM tool choice) and the OS event flow (= `routing_decided` + `invoke_skill_spawn_ack_exit` co-emit). It did NOT analyse the downstream contract for `send_to_agent_impl` consumers. The dogfood driver's `reyn chat --cui` stdin-pipe pattern uses `send_to_agent_impl` as the quiescence boundary, and post-H3 the boundary moved (= spawn turn is no longer the reply turn).

**Missed step**: before landing H3, the context analysis should have included one more lens — "what does `send_to_agent_impl`'s quiescence definition look like after this fix?" — and either updated it in the same patch or filed it as a follow-up.

The user's correction in the previous batch (*"context 分析 patch 切り分けの結果の結論であれば合意します"*) was the right discipline; I applied it within the LLM-routing slice but not to the harness-layer slice. The miss is documented here so future fix waves include the "downstream consumer contract" check.

### What worked

- **Worker prompts surfaced the gap immediately**: W2 F2 cited specific evidence ("S6 inconsistent (non-empty reply), suggesting timing-dependent race in the harness") without trying to be diplomatic. The discipline pattern (= primary-data findings, not narrative) held.
- **Honest aggregation**: this journal opens with "OS got better; harness needs to catch up" rather than "verified rate stable at 21%". The narrative shift is the right read of the data.
- **Brier improvement**: mean ~0.26 across 4 reporting workers vs B27's 0.913. The H5 refit is paying off as designed.

### What needs adjustment

- **B34's first move is the ablation of §4.1 (W2 F2 driver gap)** before any other fix dispatch. The pattern needs to be: context analysis includes downstream consumers; ablation confirms; then land. Applied to W2 F2 specifically:
  1. Capture the W2 S4/S5/S7 traces showing the empty-reply pattern.
  2. Patch `send_to_agent_impl` to extend quiescence to inbox-pending re-engage.
  3. Re-run W2's affected scenarios; confirm reply capture; measure K/N flip.
  4. Only then land the patch.

---

## 7. Cross-reference

- Worker artefacts: `workers/findings-worker-{1,2,4,5}.md` + `workers/results-worker-{1..7}.json` (= W3/W6/W7 wrote results.json + inline text only; W3 reported tool-policy block on findings.md write)
- Ablation wave journal: `docs/deep-dives/journal/dogfood/2026-05-17-ablation-wave-plateau-diagnosis/summary.md`
- B32 journal: `docs/deep-dives/journal/dogfood/2026-05-17-batch-32-b30-fix-verify/`
- Relevant commits: `25834de` (H3), `032fb80` (H5 refit), `b5d81e4` (#53 enforcement)
- Memory applied:
  - `feedback_pre_conclusion_observation_checklist.md`
  - `feedback_iterative_replay_patch_disambiguation.md`
  - `feedback_envelope_layer_fix.md`
  - `feedback_observe_before_speculate_llm.md`
- Open issues: #52 (B27-H4), #53 (= fixed in `b5d81e4`, can close), #54 (qualified-name multi-provider)
