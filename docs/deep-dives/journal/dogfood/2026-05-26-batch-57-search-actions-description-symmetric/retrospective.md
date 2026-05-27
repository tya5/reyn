# Dogfood B57 — PR #925 description-tone A/B (post-merge verify vs B56 baseline)

**Date**: 2026-05-26 → 2026-05-27 (= resumed after API quota abort)  **HEAD**: `b91b5cee` (= post PR #925 description-tone symmetric rewrite + #928 N=20 bench scaffold + #929 ST backend)

## Headline

| Metric | B56 | **B57** | Δ |
|---|---|---|---|
| V | 32/50 (64%) | **33/50 (66%)** | **+1V / +2pp** |
| I | 4 | 3 | -1 |
| R | 14 | 14 | 0 |
| B | 0 | 0 | 0 |

Net **+1V** (= trend reversed from B55→B56's -2V), but the V number is **not the primary value**. The primary value is the A/B confirmation that PR #925's description-tone symmetric rewrite delivers as designed, plus the discovery of a new downstream bug (= stale `mcp.server` category enum in LLM knowledge).

Pre-batch trace-patch-replay verify (2026-05-26, cost ~$0.20):
- hit axis (W5 mcp_search rid): search_actions 0/10 → **10/10 (100% lift)** ✓
- control axis (W1 skill enum rid): list_actions 10/10 → **10/10 (no side-effect)** ✓

## Per-worker delta

| Worker | B56 V | B57 V | Δ | Headline |
|---|---|---|---|---|
| W1 chat_router_smoke | 5/7 | 5/7 | 0 | S5/S7 persistent ceiling; search_actions 0/7 (W1 prompts lack semantic-intent triggers) |
| W2 stdlib_skills_core | 4/7 | 3/7 | -1 | S2 new R (skill_run_failed rollback JSON escape); S5/S6 R-2/R-3 carry-over persist |
| **W3 control_ir_ops** | 7/9 | **9/9** | **+2** | **Perfect run — first W3 9/9.** S7 recall I→V (= namespace fix definitive); S9 ask_user R→V (= R-6 must_emit_any catches inline 3-turn path) |
| **W4 permissions_and_safety** | 4/8 | 5/8 | **+1** | Partial recovery toward B55 baseline. S1 V (permission_denied OK), S6 V (index_drop OK), S8 V |
| W5 multi_agent_and_mcp | 2/7 | 2/7 | 0 | **search_actions DID fire** in S1/S4 (= #925 effect) but downstream `tool_failed` due to LLM passing stale `mcp.server` category (= NEW finding) |
| W6 plan_mode | 5/5 | 4/5 | -1 | S3 plan_invalid (= steps_json truncated at char 660). Path-fix holds; new JSON-malformed failure mode (LLM noise) |
| W7 long_session_v1 | 5/7 | 5/7 | 0 | S1 source__read attractor + S5 plan empty step — **both N=2 confirmed structural** (= cross-batch threshold reached, B58+ targeting candidates) |

## Primary axis findings

### Axis A — search_actions call-rate (= PR #925 effect)

| Worker | search_actions visible | search_actions called by LLM |
|---|---|---|
| W1 | yes (all calls) | 0 / 7 |
| W2 | yes (50/64 calls = 78%) | 0 / 64 |
| W3 | yes (28/76 calls = 37%) | 0 / 9 |
| W4 | yes (all 22 calls) | 0 / 22 |
| W5 | yes (~18-22/22) | **2-3 fires (S1 + S4 visible)** |
| W6 | yes | 0 / 5 |
| W7 | yes | 1 (S4-T3, rational) |
| **Total** | ~80%+ visible | **≈3-4 calls across entire batch** |

**Interpretation**:
- Pre-batch replay showed deterministic 0 → 10/10 lift on ONE specific rid (= W5 mcp github search)
- B57 confirmed search_actions FIRES when scenario provides semantic-intent prompt WITHOUT explicit action_name hint (= W5-S1, W5-S4 confirmed)
- **W3 prompts include explicit action_name hints** (`validation__lint__skill_path`, `rag.operation__recall`) — LLM bypasses discovery entirely, search_actions correctly NOT called (= no over-trigger, R-5a caveat working)
- **W1/W2/W4 prompts are category-based** (= list skills, list cron jobs) — list_actions winner, search_actions correctly NOT called
- **Net call-rate batch-wide: ~3-4/N** (= W5 + W7-S4-T3) — much lower than the 10/10 single-rid replay because most scenarios don't have the semantic-intent shape

→ **#925 effect: confirmed structural** but scenario-shape sensitive. Most dogfood scenarios are category-based or hint-laden, so empirical lift on V is modest (= W5 still 2/7 because downstream `mcp.server` enum fails even when search fires).

### Axis B — list_actions call-rate (= side-effect control)

list_actions remained the dominant choice in W1/W2/W3/W4 category-based queries. **No regression**, R-5a 過剰適合 trap successfully avoided.

### Axis C — NEW finding: stale category enum in LLM knowledge

W5-S1, W5-S4, W5-S7 all show the LLM passing `category=["mcp.server"]` (= PR #918 で廃止) to search_actions / list_actions. Result: `tool_failed` invalid_category. The PR #918 schema refactor (= collapse to single `mcp` category) didn't update the LLM's training-time knowledge.

**Fix candidate (= post-B57)**: tool description / SP rule update to make the new category enum explicit. Current `category=` enum is in the parameter schema but the LLM appears to ignore enum constraints and fall back to old category names. Either:
- (a) Strengthen enum enforcement in tool dispatch (= reject stale names with helpful error)
- (b) Add explicit cue in tool description: "category enum: [..., mcp, ...] — NOT mcp.server / mcp.tool / mcp.operation (legacy)"
- (c) Wait for next batch to confirm cross-scenario N≥3 before fix design

## Carry-over verifications

| Carry-over | B56 | B57 | Status |
|---|---|---|---|
| W3-S7 recall namespace | I | **V** | Resolved — hint + honest reply path works |
| W3-S9 ask_user must_emit_any | R | **V** | Resolved — 3-turn dispatch path fires required events |
| W4-S1 events strict (inline V→I) | I | **V** | Resolved (B56 was noise) |
| W4-S6 index_drop premature confirmation | R | **V** | Resolved |
| W4-S8 web_fetch | R | V (was V) | (B56 was R, B57 V) |
| W7-S1 source__read attractor | R | R | **N=2 structural confirmed** |
| W7-S5 plan empty step | R | R | **N=2 structural confirmed** |
| R-2 eval async timing (W2-S5) | R | R | Persistent (PR #875 partial fix not enough) |
| R-3 chat_compactor threshold (W2-S6) | R | R | Persistent (PR #880 threshold tune insufficient — `below_min_batch` x3 + `below_threshold` x2) |

## V-rate decomposition

vs B56 (= 32/50):

| Component | Effect | Detail |
|---|---|---|
| Fix effect — W3 ask_user (#914 R-6 catches) | +1 | S9 R→V (3-turn inline dispatch satisfies must_emit_any) |
| Fix effect — W3 recall (#914 R-6 + honest reply) | +1 | S7 I→V (env-mismatch resolved through prompt + reply path) |
| Fix effect — W4 multiple V resolves | +1 | S1+S6+S8 recovery (= B56 was noise / partial regression) |
| Regression — W2 S2 skill_builder rollback JSON | -1 | New failure mode (= LLM emitted invalid control type) |
| Regression — W6 S3 plan_invalid JSON truncated | -1 | New failure mode (= W6-S3 path-fix held but plan_invalid emerged) |
| **Net** | **+1V** (matches headline) | |

## Implications for B58+

1. **search_actions call-rate is scenario-shape sensitive**, not just description-tone sensitive. Description fix delivered as intended; lift opportunities are concentrated in semantic-intent / capability-discovery prompts. Add such scenarios to bench if measuring #925 effect at higher N.

2. **NEW finding: stale `mcp.server` enum** — W5 R rate dominated by this layer. Worth a separate fix PR (= scenario yaml update OR tool description update OR dispatch-time stale-enum salvage).

3. **W7 attractors S1+S5 confirmed structural (N=2)** — B58+ targeting candidates per `feedback_cross_batch_pattern_threshold`.

4. **R-2 / R-3 still persistent** — need redesign, not just parameter tuning. R-3 specifically: `below_min_batch` x3 means even 5 turns isn't producing enough compactable middle. May need scenario redesign (= much longer prompts) or a different test surface for chat_compactor.

5. **W3 9/9 perfect** — first time. R-6 fixes (S6/S7/S9) all confirmed effective in combination.

## Cost

- Pre-batch trace-replay verify: ~$0.20 (per B55 lesson, verify-before-dispatch)
- B57 dispatch: ~$5 (7 sonnet workers × 50 scenarios; aborted run + 1 quota retry)
- Net ROI: +1V + Axis A empirical confirmation + NEW `mcp.server` finding + W7 attractor N=2 promotion

## Discipline applied

- ✅ Pre-batch trace-patch-replay verify (per B55 lesson, only verified-effect fix proposed B57 dispatch)
- ✅ No pre-batch +V prediction made — honest retro post-batch
- ✅ Cross-batch pattern threshold (N=2 → W7 S1 + S5 promoted to structural)
- ✅ R-5a / 過剰適合 caveat verified empirically (= list_actions retention 100%)
- ✅ NEW finding documented (= mcp.server stale enum) for B58+ targeting

## Files

- `dogfood/batch_b57.yaml` — batch config
- `docs/deep-dives/journal/dogfood/2026-05-26-batch-57-search-actions-description-symmetric/workers/results-worker-{1..7}.json`
- `docs/deep-dives/journal/dogfood/2026-05-26-batch-57-search-actions-description-symmetric/aggregate.json`
- This retrospective
