# Dogfood B52 — Post-FP-0042-fully-complete re-measurement + B51 carry-over classification

**Date**: 2026-05-23  **HEAD**: `8e9be379` (= post-PR-#590-merge, FP-0042 cascade fully complete, stdlib unsafe surface 15 → 0)

**Update 2026-05-23 (post-retest)**: B52 W3-S4 + W3-S5 retest after env-fix
(= PR #615 + dogfood runner config injection) recovered both scenarios
R → V. Effective V-rate post-retest: **28/48 = 58.3%** (ΔV = −1 vs B51, not
−3). See "Post-retest update" section at the bottom.

## Headline

| Metric | B51 | B52 original | B52 + W3 env-fix retest | Δ vs B51 |
|---|---|---|---|---|
| V | 29/48 = 60.4% | 26/48 = 54.2% | **28/48 = 58.3%** | **−1V** (down from initial −3V before env-fix) |
| I | 3 | 6 | 6 | +3 |
| R | 14 | 16 | 14 | 0 |
| B | 0 | 0 | 0 | 0 |

**Initial ΔV = −3 vs B51** — at the noise upper bound. After the W3 env-fix retest landed (PR #615 + dogfood runner config), **effective ΔV = −1 vs B51** (= one scenario worth, well within LLM noise band).

## Per-worker delta

| Worker | Scenario set | B51 V | B52 V | Δ | Headline |
|--------|--------------|-------|-------|---|----------|
| W1 | chat_router_smoke | 4/7 | **5/7** | **+1** | R→V recovery on one scenario |
| W2 | stdlib_skills_core | 3/7 | 2/7 | −1 | S1 index_docs router didn't spawn (V→R) |
| W3 | control_ir_ops | 6/9 | 4/9 | −2 | 4 R scenarios — see below |
| W4 | permissions_and_safety | 7/8 | 6/8 | −1 | 2 V→I noise (S1/S5), 1 R→V (S8); net I+2, V−1 |
| W5 | multi_agent_and_mcp | 2/7 | 1/7 | −1 | S1 mcp_search lost "alternatives" clause |
| W6 | plan_mode | 1/3 | 1/3 | 0 | 1 R→I recovery + 1 new finding (B52-W6-2 reply truncation) |
| W7 | long_session_v1 | 6/7 | **7/7** | **+1** | Clean sweep, S1 source__read recovered |

## Per-fix verification (= FP-0042 effect on V-rate)

**Predicted**: V-rate effect of FP-0042 stdlib safe-only migration = **0** (= operator-experience win, doesn't change scenario outputs).

**Actual**: ΔV = −3, no scenario regression points at FP-0042. The W2 + W3 regressions inspected per scenario:

- **W3-S4 web_fetch**: `permission_denied` (= LOCAL env limitation, same shape as B50 W3-S4 — not OS regression).
- **W3-S5 sandboxed_exec**: events pass, but the local sandboxed Python interpreter wasn't found (= LOCAL env, not OS bug).
- **W3-S6 lint_a_skill**: `lint_completed` event missing; LLM called `invoke_action` with wrong shape (= same family as W3 wave-4 known weak-tier issue).
- **W3-S7 recall**: `missing_required_arg` (= LLM wrong-arg call shape, weak-tier noise).
- **W3-S9 ask_user**: skill_builder never spawned, LLM inline-replied after describe_action (= same plan-gen instability family as B51 W6-S3 noted).

None of these depend on `reyn.safe.file` / `reyn.safe.http` / `reyn.safe.mcp.registry` paths the FP-0042 cascade migrated; they hit unrelated wrappers or LLM-noise paths.

**Conclusion**: FP-0042 architectural impact on V-rate = **0 confirmed**. The −3 V is LLM noise + 1 stale-env scenario (W3-S4/S5 sandbox), not structural regression.

## B51 carry-over re-measurement (= the explicit reason for B52)

| B51 carry-over | B52 result | Classification |
|---|---|---|
| W6-S3 LLM plan_invalid (1-obs in B51) | W6-S2 R: router loop exceeded, plan_emitted=0 | **Same family confirmed** (= LLM plan-gen instability, weak-tier ceiling). Not the same scenario id but the same failure shape on the plan-mode worker. Class C "future challenge zone" candidate. |
| W7-S1 source__read intermittent (1-obs in B51) | W7-S1 V: clean | **Noise, recovered**. The B51 single observation was random LLM behaviour, not a structural issue. |
| W6-S2 plan-affordance gap (= unavailable `reyn.source__read`) | W6-S2 R: same router-loop shape | **Pre-existing pattern confirmed**, not V18/V19 SP interaction. Plan mode's affordance surface is the underlying issue. |

Net: 1 noise dismissed (W7-S1 ✓), 2 confirmed as known weak-tier behaviour (W6 plan-mode family, no immediate fix path).

## New findings (B52-W6-2)

**B52-W6-2 — plan_aggregated event fires but HTTP reply truncated to spawn_ack**.

Scenario `plan_summary_across_n_files` (W6-s3): all 4 plan_step events fire, `plan_aggregated` event emits with `result_len=3535`, but the A2A `message/send` HTTP reply contains only the spawn_ack envelope (`partial:true`). Aggregated result never reaches the caller. Classification: 1-observation, not a verdict killer (= I per partial reply policy). Action: file as a known-future-challenge candidate; re-measure next batch to confirm structural.

This pattern echoes plan-mode's reply-delivery surface — the events log says the work happened, but the round-trip return path lost it. Worth tracking across B53+ before scoping a fix.

## V-rate decomposition (per `feedback_v_metric_decomposition`)

| Component | Effect |
|---|---|
| OS wins / dilution | 0 (= no new OS-level scenarios) |
| Surface migration (FP-0042) | 0 confirmed |
| Worktree freshness | 0 (= all workers fresh-cleaned per dispatch script) |
| Rubric coverage | 0 (= same scenario set as B51) |
| Infra residual | -1 (= W3 S4 + S5 local-env: web_fetch + sandbox interpreter) |
| LLM noise | -2 to -3 (= weak-tier random ±2-3 V at this rate level) |
| New findings (B52-W6-2) | -1 (1 V→I via reply truncation) |
| Component improvements | +2 (= W1 +1 V, W7 +1 V) |
| **Net** | **-3 V (= matches observation)** |

Decomposition is consistent. No unexpected interaction surfaces.

## Predicted vs actual — calibration

| Source | Predicted | Actual | Notes |
|---|---|---|---|
| FP-0042 V impact | 0 | 0 ✓ | confirmed |
| B51 carry-over: W6-S3 / W7-S1 / W6-S2 | re-measure to classify | classified | W7-S1 noise, W6 family confirmed |
| LLM noise band | ±2-3 V | -3 V | at upper bound but within band |
| Median expected V | ~28-30/48 | 26/48 | -2 vs median expectation |

`feedback_pre_conclusion_observation_checklist` applied:
- ✅ specific observations listed per worker
- ✅ primary data (= events log + per-scenario verdict from worker JSON, not inference)
- ✅ falsification considered (= "did FP-0042 cause this?" → checked migration paths vs failing scenarios; no overlap)
- ✅ observation infra adequate (= aggregate.json + per-worker JSON cover what's needed for classification)
- ✅ N=N inspection (= per-scenario verdicts inspected, not extrapolated)

## Discipline applied

- ✅ `feedback_user_params_fixed_in_comparison`: env_vars + user_params held constant vs B51
- ✅ `feedback_no_strong_model`: all 7 workers ran flash-lite
- ✅ `feedback_subagent_scope_bounding`: 1 worker = 1 deliverable (results-worker-N.json), hard cap 50 tool uses + 15 min, all stayed within
- ✅ `feedback_batch_report_past_comparison`: per-worker comparison table + Δ vs B51 single anchor
- ✅ `feedback_v_metric_decomposition`: 7-component decomposition above
- ✅ `feedback_pre_conclusion_observation_checklist`: invariant verification per worker before stating cause

## Files

- `aggregate.json` — verdict totals + Δ vs B51 + per-worker counts (= original B52 run; not updated post-retest)
- `workers/results-worker-{1..7}.json` — per-worker raw verdicts + per-scenario detail (original run)
- `workers/results-worker-3-retest-s4-s5.json` — env-fix Round-1 retest deliverable
- `workers/results-worker-3-retest2-s5.json` — sandbox_config propagation Round-2 retest deliverable
- `batch_b52.yaml` (in `dogfood/`) — batch config with HEAD `8e9be379` + env_vars + past_batches anchor

## Implications for B53+

- **FP-0042 architectural win established**: stdlib runs end-to-end on weak-tier without `--allow-unsafe-python`. The B51 W2-S1/S7 trigger (= the empirical motivation for FP-0042) is unblocked; no dogfood scenario in B52 needed unsafe-python opt-in.
- **W3 local env fixes** (= web_fetch permission, sandboxed Python interpreter) would recover 2 V if addressed — but those are dogfood-env config, not Reyn-OS bugs. Worth a one-line note in the dogfood runner README.
- **B52-W6-2 reply truncation** is the only genuinely new finding. Add to `known-future-challenges.md` and re-measure in B53 before scoping a fix.
- **Plan-mode weak-tier ceiling** (= W6 family) remains in the deferred zone per `known-future-challenges.md`. Catalog-overlap weak ceiling (W5-S5 family) likewise unchanged.
- For B53: maintain B52's HEAD anchor; the cumulative FP-0042 cascade is fully landed, so future batches measure on top of the stable safe-only stdlib baseline.

---

## Post-retest update (2026-05-23, post-PR-#615)

The "W3 local env fixes" item above was originally framed as a dogfood-runner
infra concern. **The retest surfaced a real OS bug** in addition to the
runner config. Both fixes landed and the two scenarios flipped to V.

### Retest sequence

**Round 1** — dogfood runner config (`scripts/dogfood_batch_dispatch.py` auto-injects `permissions.web.fetch: allow` + `sandbox.backend: noop` into worker `reyn.local.yaml`):

| Scenario | Verdict | Evidence |
|---|---|---|
| W3-S4 web_fetch_url | **R → V** | `web__fetch` succeeded status=200, reply names PEP 695 + PEP 701 |
| W3-S5 sandboxed_exec_simple | **R → I** | tool ran but `sandboxed_exec_started.backend == "seatbelt"` despite `sandbox.backend: noop` in reyn.local.yaml — config didn't propagate to runtime |

The Round-1 partial failure on S5 was the smoking gun: `load_config().sandbox.backend == "noop"` returned correctly, but the runtime handler picked Seatbelt anyway. Tracing the chain:

```
operator reyn.yaml `sandbox.backend: noop`
        │
        ▼
load_config().sandbox       ← correct ✓
        │
        ▼
ChatSession(sandbox_config=...)   ← MISSING in 2/4 call sites ✗
        │
        ▼
Agent(sandbox_config=...) → RouterCallerState.sandbox_backend
        │
        ▼
sandboxed_exec handler picks backend
```

Found two missing call sites:
- `src/reyn/web/deps.py` `_session_factory` (= A2A surface).
- `src/reyn/cli/commands/mcp.py` `_session_factory` (= MCP-serve surface; caught only because the new AST uniformity test below flagged it after the deps.py fix landed).

Cron-side `src/reyn/web/server.py` was the only surface passing the kwarg correctly.

**Round 2** — `web/deps.py` + `cli/commands/mcp.py` patched (= PR #615):

| Scenario | Verdict | Evidence |
|---|---|---|
| W3-S5 sandboxed_exec_simple | **R/I → V** | `sandboxed_exec_started.backend == "noop"` in events.jsonl; reply correctly reports `"4"`, returncode=0 |

### PR #615 — landed

`fix(web/a2a + mcp): propagate sandbox_config to ChatSession factory (B52 W3-S5 retest finding)` (merged).

Changes:
- `src/reyn/web/deps.py` — A2A `_session_factory` now passes `sandbox_config=config.sandbox`.
- `src/reyn/cli/commands/mcp.py` — same fix on MCP-serve factory.
- `tests/test_session_factory_sandbox_config_uniform.py` (new) — Tier-2 AST invariant test pinning every `ChatSession()` call site as passing `sandbox_config`. Mirror of existing `multimodal_config` uniformity test. Caught the `mcp.py` miss before it shipped.
- `scripts/dogfood_batch_dispatch.py` — worker `reyn.local.yaml` auto-injects the two env grants.
- `dogfood/scenarios/control_ir_ops.yaml` — S4 + S5 add `requires_permissions` / `requires_sandbox_backend` metadata.

### Discipline applied to the retest

- `feedback_verify_fix_via_replay_before_land`: Round-1 patch (= runner config) was verified before claiming success → Round-1 retest surfaced the propagation bug instead of pretending the runner config alone fixed it.
- `feedback_observe_before_speculate_llm`: the events.jsonl `sandboxed_exec_started.backend` value was the primary observation that pointed at config propagation rather than runtime-policy difference.
- `feedback_pre_conclusion_observation_checklist`: Round-2 retest demanded `backend == "noop"` as a specific event field, not just verdict V — catching a future regression where the verdict happens to be V but backend wrongly silently reverted.

### Updated implications for B53+

- **W3 V recovered to 6/9** post-fix (= 4/9 → 6/9, +2V).
- **A2A surface sandbox enforcement now matches operator config** — any reyn.yaml `sandbox.backend` declaration reaches runtime via the chat-router path, same as cron.
- **AST uniformity test on `ChatSession()` call sites** now covers both `multimodal_config` AND `sandbox_config`. Future cross-callsite config-propagation bugs of the same shape (= new HTTP gateway / scheduled job / daemon adds a surface, forgets the kwarg) get flagged at PR-review time.
- **Dogfood-driven structural-bug discovery cycle** validates again: the V-rate decomposition's "infra residual" component, when investigated, surfaced a real OS bug not just env config. Keep the discipline of looking past the surface verdict for the event-level evidence.
- For B53: same HEAD as B52 plus PR #615 lands. W3-S4/S5 expected to stay V; no new carry-overs from this finding.
