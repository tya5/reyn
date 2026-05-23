# Dogfood B53 — Post-PR-#615 sandbox_config-fix verification + B52-W6-2 axis re-measurement

**Date**: 2026-05-23  **HEAD**: `5f46af7b` (= post-PR-#615 + #617 merge, sandbox_config kwarg now propagated through A2A + MCP-serve ChatSession factories, AST-uniformity test landed)

## Headline

| Metric | B52 aggregate | B52 + retest (post-PR-#615) | B53 | Δ vs B52 aggregate | Δ vs B52 post-retest |
|---|---|---|---|---|---|
| V | 26/48 = 54.2% | 28/48 = 58.3% | **29/48 = 60.4%** | **+3V** | **+1V** |
| I | 6 | 6 | 5 | -1 | -1 |
| R | 16 | 14 | 14 | -2 | 0 |
| B | 0 | 0 | 0 | 0 | 0 |

**Primary axis (PR #615 sandbox_config fix)**: confirmed in full-batch run. W3-S4 web_fetch + W3-S5 sandboxed_exec both V on the first non-retest dispatch since the fix, matching the retest-validated state. The fix sticks under normal batch conditions.

**Secondary axis (B52-W6-2 reply truncation)**: did **not** recur as the same failure shape, but the same scenario (`plan_summary_across_n_files`) presents a *different* plan-mode failure in B53. See "B52-W6-2 axis re-measurement" below — reclassified as one shape within a broader plan-mode reply-path family rather than a single isolated finding.

## Per-worker delta

| Worker | Scenario set | B52 V | B53 V | Δ | Headline |
|--------|--------------|-------|-------|---|----------|
| W1 | chat_router_smoke | 5/7 | 5/7 | 0 | Stable. S5/S7 R (= same as B52, weak-tier ceiling). |
| W2 | stdlib_skills_core | 2/7 | 2/7 | 0 | Same I/R mix shape (1 I→R, 1 R→I net 0). |
| W3 | control_ir_ops | 4/9 | **6/9** | **+2** | **PR #615 confirmed**: S4 web_fetch V (env grant via dispatch script) + S5 sandboxed_exec V (sandbox.backend=noop reaches handler). |
| W4 | permissions_and_safety | 6/8 | **7/8** | **+1** | One R→V recovery (LLM noise direction). |
| W5 | multi_agent_and_mcp | 1/7 | 2/7 | +1 | LLM noise (= weak-tier MCP catalog-overlap ceiling). |
| W6 | plan_mode | 1/3 | 1/3 | 0 | V flat; I/R shape changed (= 1 I→R via different plan-mode failure shape). |
| W7 | long_session_v1 | 7/7 | 6/7 | -1 | Single upstream LiteLLM/Gemini `BadRequestError` flake on s3 (not Reyn regression). |

## PR #615 sandbox_config fix — primary axis verification

**Fix recap**: A2A + MCP-serve `ChatSession()` factory calls were missing the
`sandbox_config=config.sandbox` kwarg, so `reyn.yaml`'s `sandbox.backend`
declaration never reached the `sandboxed_exec` handler via the chat-router
path (only the cron-side factory was correct).

**B53 verification**:
- W3-S4 `web_fetch_url`: **V**. `web_fetch_started` + `web_fetch_completed` both fire; reply summarises Python 3.12 features. The `permissions.web.fetch: allow` injected by `scripts/dogfood_batch_dispatch.py --setup-worktrees` reaches the runtime.
- W3-S5 `sandboxed_exec_simple`: **V**. `sandboxed_exec_started` + `sandboxed_exec_completed` both fire; reply is `"4"`. The `sandbox.backend: noop` injected by the same dispatch script reaches the handler — confirming `sandbox_config` now propagates end-to-end.

This is the first full-batch dispatch (= not a targeted retest) where both scenarios run green. The fix is not retest-only behaviour.

**No surprises on PR #618 axis** (= e2e-coder safe.http per-host gate, landed since B52): no dogfood scenario directly exercises this surface; W3-S3 web_search ran cleanly, no permission-model interactions surfaced.

## B52-W6-2 axis re-measurement — reclassified

B52 finding: W6 s3 `plan_summary_across_n_files` — events all met
(`plan_aggregated` fired with `result_len=3535`), but HTTP reply truncated
to spawn_ack envelope (`partial:true`). Verdict was **I**. Classification:
1-observation, await B53 to classify noise vs structural.

B53 same scenario:
- **Different failure**: `plan` tool returned `status=error kind=plan_invalid` (= malformed `steps_json` at col 387) **before** any `plan_emitted` event. No `plan_aggregated` event.
- HTTP reply: literal `"plan\n\nplan"` (179 bytes) — neither spawn_ack envelope nor aggregated result. Plain degenerate string.
- Verdict: **R** (rubric `refs_all_three_files` / `at_least_4_pillars` all false).

**Classification**: the "single observation" framing was too narrow. B52-W6-2 (= reply-path truncation post-`plan_aggregated`) and B53-W6-s3 (= `plan_invalid` pre-`plan_emitted` + degenerate reply) are **two different shapes of plan-mode reply-path instability on the same scenario**. The underlying surface — plan-mode's reply construction + plan-step generation under weak-tier — is unstable in at least two distinct ways for `plan_summary_across_n_files`.

This is not the same "noise vs structural" binary the B52 retro framed. It's: **same surface, multiple failure shapes, weak-tier ceiling**. Same family as W6-S2 (= router-loop-exceed on `plan_explain_with_code_references`, recurring in B53 as B52 R→R). Filing as plan-mode reply-path family for B54+ scoping; not actionable as a single targeted fix.

## V-rate decomposition (per `feedback_v_metric_decomposition`)

vs B52 post-retest (= 28/48):

| Component | Effect | Detail |
|---|---|---|
| OS wins / dilution | 0 | No new OS scenarios. |
| Surface migration | 0 | No new migration since B52. |
| Worktree freshness | 0 | All workers fresh-cleaned per dispatch script. |
| Rubric coverage | 0 | Same scenario set as B52. |
| Infra residual | 0 | Dispatch script env-injections (web.fetch + sandbox.backend) held across B53; no new infra debt observed. |
| LLM noise | +1 to +2 | W4 +1V, W5 +1V recoveries; W6/W2 flat at known ceilings. |
| New / re-measured findings | -1 | W6-s3 (B52-W6-2 axis) regressed I→R via different failure shape. |
| Component improvements | 0 | No new structural fix landings between B52 and B53 beyond the already-counted PR #615 (which is in the B52 post-retest baseline). |
| Upstream env flake | -1 | W7-s3 LiteLLM/Gemini `BadRequestError` on turns 2-5. |
| **Net** | **+1V** (matches +1V vs B52 post-retest) | |

vs B52 aggregate (= 26/48, +3V): same decomposition plus +2 for the PR-#615 effect already captured in the post-retest baseline (= W3 S4 + S5 conversion in the full-batch run = primary axis confirmation, but it's the same 2 V the retest already counted, not double-counted).

## Predicted vs actual — calibration

| Source | Predicted | Actual | Notes |
|---|---|---|---|
| PR #615 sandbox_config-fix effect | W3-S5 stays V in full batch | W3-S5 + W3-S4 both V | ✓ confirmed |
| LLM noise band | ±2-3 V | +1V net (+2 from W4/W5, -1 from W6, -1 from W7 upstream) | within band |
| B52-W6-2 noise vs structural | re-measure to classify | reclassified: same surface, multiple shapes | finer-grained than the binary |
| Median expected V | 28-30/48 | 29/48 | ✓ matches median |

`feedback_pre_conclusion_observation_checklist` applied:
- ✅ Per-worker observations listed with specific event counts + reply shapes.
- ✅ Primary data: events log + per-scenario worker JSON, not inference chains.
- ✅ Falsification: B52-W6-2 expected to "recur or dismiss as noise" — actively looked for the *same* failure shape, found a *different* one. Reframed the classification rather than overclaiming.
- ✅ Observation infra adequate: events log + reply text + worker JSON suffice for shape classification.
- ✅ N=N inspection per worker (= all 7 worker deliverables read, no extrapolation from sub-set).

## Discipline applied

- ✅ `feedback_user_params_fixed_in_comparison`: env_vars + user_params held constant vs B51/B52 (`hot_list_n=10`, `flash-lite`, `REYN_EMPTY_STOP_RETRY=1`, `REYN_SPAWN_ACK_TO_LLM=1`).
- ✅ `feedback_no_strong_model`: all 7 workers ran flash-lite.
- ✅ `feedback_subagent_scope_bounding`: 1 worker = 1 deliverable, hard caps (≤50 tool uses, ≤15 min) honoured. W2 first dispatch stuck in notification-loop without writing — re-dispatched with explicit "write the file" instruction, completed successfully (= recovery path documented for future reference, not a finding).
- ✅ `feedback_batch_report_past_comparison`: per-worker comparison table + Δ tracked against B52 aggregate AND B52 post-retest (= dual anchor for clarity, since PR #615 retest converted 2 R→V mid-cycle).
- ✅ `feedback_v_metric_decomposition`: 8-component decomposition above.
- ✅ `feedback_pre_conclusion_observation_checklist`: B52-W6-2 reclassification specifically guarded against overclaim ("not recurring" would have been wrong; the surface is unstable in multiple shapes).

## Files

- `aggregate.json` — verdict totals + per-worker counts + Δ vs B52
- `workers/results-worker-{1..7}.json` — per-worker raw verdicts + per-scenario detail
- `dogfood/batch_b53.yaml` — batch config + carry-over notes

## Implications for B54+

- **PR #615 axis established**: sandbox_config propagation works end-to-end in full batches. No further retest needed; future regressions would surface immediately on the W3-S4/S5 axis.
- **Plan-mode reply-path family** (= W6-s2 router-loop + W6-s3 plan-invalid+degenerate-reply + B52-W6-2 reply-truncation-post-aggregate): three observed shapes on the same plan_mode worker, all weak-tier behaviour. Not actionable as a single fix; track over B54+ for shape-frequency mapping before scoping. Candidate for "known weak-tier ceiling" doc once 2-3 more batches confirm.
- **W7 upstream LLM env flakes** (= LiteLLM/Gemini `BadRequestError` on s3): if this recurs in B54+, classify as infra residual; one observation is not enough to re-scope dispatch behaviour.
- **W2 sub-agent stuck-in-notification-loop**: occurred on first W2 dispatch this batch; re-dispatch with explicit "write deliverable" recovered. If this pattern recurs across batches, consider hardening worker prompt boilerplate (= explicit Step N "WRITE THE FILE" line at end). Single observation, not actionable yet.
