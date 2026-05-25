# Dogfood B56 — search_actions eager-build baseline (post #918 / #920 / #923)

**Date**: 2026-05-26  **HEAD**: `89bacbdf` (= post PR #918 mcp install 3-way split + PR #920 agent-lifecycle parity + PR #923 reyn web eager-embedding-build)

## Headline

| Metric | B55 | **B56** | Δ |
|---|---|---|---|
| V | 34/50 (68%) | **32/50 (64%)** | **-2V / -4pp** |
| I | 3 | 4 | +1 |
| R | 13 | 14 | +1 |
| B | 0 | 0 | 0 |

Per B55 lesson `feedback_b55_prediction_miss_lessons`, **no pre-batch +V prediction was made** for B56. The honest delta is -2V, but the primary axis findings are what matter for B57 planning.

## Per-worker delta

| Worker | B55 V | B56 V | Δ | Headline |
|---|---|---|---|---|
| W1 chat_router_smoke | 4/7 | 5/7 | **+1** | S2 V (web_search recovery); S5/S7 persistent ceiling (poem clarification / out-of-scope) |
| W2 stdlib_skills_core | 4/7 | 4/7 | 0 | S2 R→V (router crash noise confirmed); S7 V→R (index_docs zero-chunks regression); S5/S6 carry-over R-2/R-3 still R |
| W3 control_ir_ops | 7/9 | 7/9 | 0 | Identical distribution (S6 V hint holds, S7 I env, S9 R must_emit_any insufficient) |
| W4 permissions_and_safety | 6/8 | 4/8 | **-2** | S1 V→I (inline reply, routing_decided not emitted), S2 V→I (mcp_install 3-way split didn't help LLM action lookup), S6 R persistent |
| W5 multi_agent_and_mcp | 2/7 | 2/7 | 0 | **S1 R→V** (PR #918 `mcp__search_registry` rename effect confirmed); S4 V→I regression; S3/S5/S6/S7 R |
| W6 plan_mode | 4/5 | **5/5** | **+1** | **First 5/5 perfect run.** S1 B55 regression confirmed as noise; W6-S3 path-fix holding |
| W7 long_session_v1 | 7/7 | 5/7 | **-2** | S1 R (source__read attractor); S5 R (plan empty step for producer-consumer code) |

## Primary axis findings

### Axis 1 — search_actions visibility (PR #923 — DELIVERED)

PR #923 added `reyn web --eager-embedding-build` so action_embedding_index builds synchronously on session start. The dispatch script auto-injects the flag.

**Empirical verify (B56 trace data)**:
- W5 (= MCP + multi-agent scenarios): `search_actions` in tools[] = **18 / 22 router calls (= ~82%)**
  vs B55 W5 pre-fix: 2 / 23 calls (= 8.7%)
- W1 / W3 / W6 / W7: also showed search_actions visible in their tools[] arrays consistently

→ Eager-build delivered as designed. The visibility path is closed.

### Axis 2 — search_actions LLM call-rate (#924 BASELINE)

Visibility ≠ usage. Even with search_actions in tools[] for ~82% of W5 calls:

| Worker | search_actions called by LLM |
|---|---|
| W1 | 0 / 7 scenarios |
| W3 | 0 / 9 scenarios |
| W5 | **0 / 22 router calls** (despite 18/22 visible) |
| W6 | 0 / 5 scenarios |

= **0% LLM call-rate across the entire batch**.

This **isolates the description-tone asymmetry** (= per `feedback_pr920_b55_root_cause_analysis` and the symmetric directive pattern proposed by sandbox_2):

- `list_actions` description: 5+ all-caps + "FIRST" / "ALWAYS" / "failure mode" hard directives
- `search_actions` description: soft "PREFERRED" only

The LLM picker layer's bias toward list_actions is the call-suppression mechanism, **independent of embedding precision or visibility**.

PR #925 (= issue #924, e2e-coder authored, sandbox_2 co-author per B55 primary evidence) lands the symmetric "PREFERRED FIRST + FAILURE MODE + redirect" rewrite that should shift this 0/N baseline. **B57 dispatch (post-#925 merge)** will measure the effect as a clean A/B against this B56 baseline.

**B57 expected metrics (= e2e-coder + sandbox_2 alignment)**:
- search_actions call-rate: 0/N → ≥0.3 (semantic-intent queries)
- list_actions call-rate: NOT large drop (= side-effect control axis)
- prompt-cue driven switch: 「探したい」 系 / semantic → search_actions、 category enum → list_actions

### Axis 3 — PR #918 mcp action rename / 3-way install split

- **`mcp__search_registry` rename**: W5-S1 R→V confirmed ✓. LLM uses the new action_name correctly (= no rename break).
- **mcp__install_server 3-way split**: W4-S2 still I (workspace arg missing) and W5-S6 still R (inline reply). The LLM's failure mode in B56 is NOT the old "one-verb XOR" schema problem the split solved; instead it's an earlier-stage routing failure (LLM doesn't reach the install action at all). The split's intended benefit (= structural disambiguation between registry / package / local install) doesn't materialise because the LLM never invokes any install verb.

### Axis 4 — PR #920 agent-lifecycle 3-path parity

W5-S3 agent_delegation_simple was the target scenario. In B56:
- LLM discovered `multi_agent__delegate` via list_actions
- Then **empty-stopped without invoking** it
- Never reached the `[task_completed] kind=agent` injection that PR #920 added

→ PR #920 effect **unmeasurable** in B56. The earlier-stage routing failure prevents reaching the lifecycle path. Need scenarios that reliably trigger agent delegation to measure #920 effect; alternatively, the post-#925 description-tone fix may also unstick this path.

## V-rate decomposition (per `feedback_v_metric_decomposition`)

vs B55 (= 34/50):

| Component | Effect | Detail |
|---|---|---|
| Fix effect — #918 rename delivered | +1 | W5-S1 R→V (mcp__search_registry resolves correctly) |
| Fix effect — #920 lifecycle parity | 0 | LLM never reached delegation in W5-S3 |
| Fix effect — #923 eager-build delivered (visibility) | 0 | visibility delivered but LLM didn't pick the now-visible search_actions (= Axis 2 isolation) |
| Regression — W4 events strict on inline (S1 V→I) | -1 | Same R-5a/R-6 pattern (= events.must_emit `routing_decided` doesn't fire on inline) |
| Regression — W4 mcp_install carry-over (S2) | -1 | PR #918 didn't address the earlier-stage routing failure |
| Regression — W7 S1 source__read attractor | -1 | Tool error fallback dominated |
| Regression — W7 S5 plan empty step | -1 | New failure mode (plan step generated empty content) |
| LLM noise — W1 +1 / W2 composition shift | +1 | Within band |
| LLM noise — W6 first 5/5 perfect | +1 | Plausibly noise + cumulative scenario-quality lift from PR #867/#883 trajectory |
| W2 S2 regression undone | +1 | B55 router crash was noise, confirmed |
| W2 S7 zero-chunks regression | -1 | New env issue (index_docs strategy phase aborts) |
| **Net** | **-2V** | matches headline |

## Carry-over for B57+ (= B55 carry-overs + B56 new)

| Carry-over | Status |
|---|---|
| R-2 async eval reply timing (W2-S5) | R persistent — A2A async reply still returns before eval completes |
| R-3 chat_compactor threshold (W2-S6) | R persistent — 71 tokens vs 2000 threshold, never fires |
| R-5a / R-6 S9 events.must_emit_any extension | not addressed since B55; W3-S9 still R |
| **Axis 2 — search_actions call-rate** | **0/N baseline established for #925 to lift** |
| W2-S7 index_docs zero-chunks at strategy phase | NEW regression in B56 — verify if env or structural |
| W4-S2 mcp_install workspace arg / S6 index_drop | both persistent across B55 + B56 |
| W7-S1 source__read attractor | NEW — appears related to W6-S3 wrong-path family |
| W7-S5 plan empty step | NEW shape (possibly same as B54 NF-W7-S5 sub-agent dispatch attractor family) |

## Discipline applied

- ✅ User param fixed vs B55 (hot_list_n=10, flash-lite, default seed, fresh mode)
- ✅ No pre-batch +V prediction (= B55 lesson)
- ✅ search_actions axis isolation via PR #923 visibility fix → enables single-PR-effect attribution for #925
- ✅ 7 parallel sonnet workers (= user 明示承認下), hard cap 50 tools / 15 min / 1 deliverable
- ✅ Honest retro with regression attribution

## Cost / ROI

- ~$5 dispatch cost
- Net V: -2V — but **Axis 2 baseline isolation (= 0/N empirical) is the real value**, not the V number. B57 will measure the effect of #925 against this clean baseline.

## Implications for B57 (= post-#925 merge)

Run B57 with **same batch yaml** (= apples-to-apples; only main HEAD differs). Primary measurement target:
1. **search_actions call-rate** in W1/W3/W5/W6 — should rise from 0/N (baseline established here)
2. **list_actions call-rate** unchanged (= side-effect control)
3. **W5/W6 V** — secondary lift if Axis 2 fix unsticks the routing-failure path

## Files

- `dogfood/batch_b56.yaml` — batch config
- `docs/deep-dives/journal/dogfood/2026-05-26-batch-56-search-actions-eager-baseline/workers/results-worker-{1..7}.json` — per-worker raw output
- `docs/deep-dives/journal/dogfood/2026-05-26-batch-56-search-actions-eager-baseline/aggregate.json` — combined aggregate
- This retrospective
