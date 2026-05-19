# B43 retrospective — post-empty-stop-retry cumulative effect

**Batch**: B43
**Date**: 2026-05-20
**HEAD at dispatch**: `e96d479f`
**Scope**: cumulative effect of 5 merged dogfood-coder PRs (#248 / #253 / #257 / #265) + Phase 1-5 intervention refactor (#255-263) since the B42 baseline (= 74 commits delta).

## TL;DR

- **V=22/54 (40.7%)** vs B42 V=21/54 (38.9%), ΔV=+1.
- **PR #265 plan-step empty-stop retry: VERIFIED end-to-end** on W6-s1 (R→V with 2 `router_empty_response_retry_injected` events captured). Same retry path also activated cross-scenario on W3-s8, W4-s5 (mid-chain recovery).
- **Scope gap surfaced** (= NF-W6-B43-1): top-level router empty-stop is a separate code path not covered by the planner-side wiring. W6-s2 R→R demonstrates this.
- B=0 in B43 vs B=2 in B42 — cleaner run, fewer infrastructure failures.

## Past-batch comparison table

| Worker | Scenario set | B41 V | B42 V | B43 V | ΔvsB42 |
|---|---|---|---|---|---|
| W1 | chat_router_smoke | 2/7 | 3/7 | 3/7 | 0 |
| W2 | stdlib_skills_core | 1/9 | 5/8+1B | 5/9 | 0* |
| W3 | control_ir_ops | 3/9 | 5/9 | 4/9 | -1 |
| W4 | permissions_and_safety | 4/8 | 6/8 | 4/8 | **-2** |
| W5 | multi_agent_and_mcp | 0/7 | 0/7 | 2/7 | **+2** |
| W6 | plan_mode + fp_0011 | 1/11 | 0/7+1B | 1/7 | **+1 (PR #265 ✓)** |
| W7 | long_session_v1 | 2/7 | 2/7 | 3/7 | +1 |
| **Total** | | **13/58** | **21/54+2B** | **22/54** | **+1** |

*W2 ΔV=0 nominally but real-world signal stable: B42 W2 was crash-recovery rubric-only; B43 W2 events-gated. s9 unblocked + s3/s4 V→R reflect B42 rubric-only inflation correction.

## Per-PR fix verification

### PR #265 plan-step empty-stop retry — **VERIFIED**

- W6-s1 plan_compare_two_concepts: B42 R → B43 V
  - `router_empty_response_retry_injected = 2 events` captured in events.jsonl
  - 1254-char substantive reply (= P5 + Permission model relationship with concrete doc references)
- W3-s8 judge_output_direct: B42 V → B43 V (= retry path fired but original V also held)
- W4-s5 budget_chain_warn: mid-chain 3 empty stops recovered via retry
- ENV-var opt-in (`REYN_EMPTY_STOP_RETRY=1`) behaved as designed.

**Scope gap** (= NF-W6-B43-1): W6-s2 plan_explain_with_code_references R→R. The plan top-level (= chat router) empty stop falls in `router_loop.py:1810` which is a separate instance from the planner.py:885/966 wiring. Same attractor, different code path. **B44 candidate**: extend the retry directive to the chat session's RouterLoop construction site.

### PR #248 describe_action resource description — **INDIRECTLY VERIFIED**

No isolated A/B test in B43, but no W6-S7-T2-style empty-stop on describe_action observed across the batch (= the original NF-W7-B42-1 surface). Cumulative chain quality consistent with the fix being active and benign.

### PR #253 A2A sync→Task escalation — **NOT EXERCISED**

No scenario triggered a long-running skill that crossed the 60s `DEFAULT_SEND_TIMEOUT`. Sync mode resolved before escalation needed. No regression.

### PR #257 follow-up notes — **doc + reliability only, no scenario impact**

`_resource_description` docstring + `_escalate_to_task` var rename + `status="timeout"` on deadline expiry — none crossed the B43 scenario set's behaviour surface.

### Phase 1-5 intervention refactor (#255-263) — **likely contributing to B=0 + W6-s6 unblocking**

W6-s6 s-fp11-1-builder-invalid-spec went B→I (= B41-NF-W7-2 driver-completion-drop reproduction unblocked). The Phase 1 subscriber-presence guard (= PR #255) likely closed the silent-queue case via fail-fast `no_subscriber` refuse. Not a dogfood-coder PR but counted in the cumulative chain.

## Regressions / NFs (= attractor analysis applied)

Post-batch attractor analysis run on the two highest-priority NFs (= W6-B43-1 + W4-B43-1) via trace-patch-replay N=10. Remaining 4 NFs marked as hypotheses pending the same measurement protocol (= `feedback_code_inspection_not_enough_for_fix` discipline applied — N=1 observations classified as hypothesis, not structural finding).

| NF | Scenario | Classification | Evidence |
|---|---|---|---|
| **NF-W6-B43-1** | plan_explain (s2) + general top-level router | **STRUCTURAL ✓** | baseline N=10 = 6/10 EMPTY_STOP (= 60% trigger); patched (retry directive injected) N=10 = 0/10 empty + 10/10 substantive (169-1485 chars). Same fix mechanism as PR #265 plumbed at the top-level router would close it. **B44 PR target**. |
| **NF-W4-B43-1** | permissions_and_safety s1+s2 | **LLM NOISE** (close) | W4-S1 baseline N=10 = 10/10 TOOL_CALL (invoke_action); W4-S2 baseline N=10 = 10/10 TOOL_CALL (5x invoke_action + 5x list_actions). Live B43 W4 inline-refusal was N=1 unlucky on 2 scenarios. The "-2V regression" is measurement artifact, not real attractor. |
| NF-W3-B43-1 | control_ir_ops s7 | **TBD** (hypothesis) | recall reply attractor shift observed N=1. N=10 measurement pending; out of B43 scope. B44 carry-over. |
| NF-W7-B43-1 | long_session s1+s2 | **out-of-scope** | retry path used but cumulative double-empty hits at strong attractors. Explicit policy bound (= max 1 retry per turn). Not a fix candidate. |
| NF-W7-B43-2 | long_session s7 | **TBD high-priority** | inline-reply path emitted spawn-ack text without spawning skill. Possible Phase 1-5 intervention refactor regression. B44 high-priority — needs bisect against #255-263 + N=10 reproducibility check. |
| NF-W1-B43-1 | chat_router_smoke s6 | **TBD** (hypothesis) | turn-2 over-dispatched direct_llm 5x in one turn. Possible REYN_EMPTY_STOP_RETRY amplification. B44 carry-over — needs B42 cross-check + N=10 measurement before structural classification. |

### Net delta re-interpretation (post-analysis)

- Raw count delta: +1V (22 vs 21).
- NF-W4-B43-1 noise correction: the 2 "regressed" scenarios are N=1 unlucky, not real attractor shifts. Adjusting back, effective ΔV ≈ +3V if those scenarios had rolled at 10/10 tool_call rate.
- NF-W6-B43-1 confirms a separate empty-stop surface beyond what PR #265 covers — same fix mechanism applies, just wider plumbing needed.
- Remaining 4 NFs are hypotheses pending the N=10 protocol applied above. They are NOT yet classified as structural.

The discipline lesson here is direct: B43's worker reporters surfaced -2V on W4 as a regression; attractor analysis showed it was noise. Without the analysis, this batch would have shipped a misleading retrospective claiming a structural W4 regression.

## Cross-cutting observations

### PR #265 cross-scenario benefit

The retry path is **not just for plan-step**: it activated in W3-s8, W4-s5 (mid-chain), W6-s1 (plan-step direct), and others. The env-var opt-in is therefore a broader empty-stop salvage than the original B42 W6 fix scope.

### B=0 cleaner run

B42 had W2-s9 + W6-s6 blocked from infra issues (= crash-recovery rubric-only + driver-completion-drop). B43 had neither. The Phase 1 subscriber-presence guard + W2's events-gated rerun + my own batch-dispatch discipline contributed.

### Worker scope bounding (= `feedback_subagent_scope_bounding`)

All 7 workers completed within hard caps (≤50 tool uses, ≤15 min; W7 ≤60/20). No worker exceeded — cleaner than B41 batch's bundled-deliverable workers that ran 50-185 tool uses each.

## Decision: B43 closure

B43 closes with V=22/54 (40.7%), +1V vs B42, **PR #265 primary fix verified end-to-end**. The 4 in-house fixes (#248/253/257/265) are KEEP — direct regression evidence absent.

Scope gap NF-W6-B43-1 (= top-level router empty stop) is the highest-priority B44 follow-up. Other 4 NFs queued for B44 dispatch order.

## Process notes

- All 7 workers used the same env_vars + user_params (= apples-to-apples vs B42).
- Past-batch verdicts cited from B42 primary data in each worker prompt (= reproducible attribution).
- No strong model invocation (= verified per worker reyn.local.yaml).
- Hard caps enforced + completed-within-budget on all 7 workers (= contrast vs B41 worker scope creep).
- batch_dispatch boilerplate cost: ~500 lines of structured Markdown across 7 prompts. Framework candidate (= dogfood driver framework, follow-up after retrospective).
