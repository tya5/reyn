# B42 retrospective — B40-v2 cumulative + carry-over fixes verification

**Batch**: B42
**Date**: 2026-05-19
**HEAD at dispatch**: `79748ac9`
**Scope**: measure cumulative effect of three B41 carry-over fixes — PR #204 (run_skill path), PR #207 (driver wait-for-skill-completion), PR #221 (describe_action `_post_text` directive).

## TL;DR

- **V=21/54 (38.9%)** vs B41 V=13/58 (22.4%) and B39 V=21/58 (36.2%).
- Apparent ΔV=+8 vs B41, BUT after caveat decomposition: ~ΔV=+4 confirmed (W3 +2, W4 +2), ~ΔV=+4 provisional (W2 offline rubric-only inflation pending events re-verification).
- **PR #221 VERIFIED win** on W7-S7-T2 (B41 empty-stop → B42 1820 chars substantive). Strong direct evidence.
- **PR #207 PARTIALLY VERIFIED**: base case fixed (driver runs to completion), but EXPOSED a previously-hidden empty-stop attractor in W7-S2 (= visibility win, not regression).
- **PR #204 NOT EXERCISED** by this scenario set — no eval/short-form path scenarios; W5-S6 mechanism change flagged for investigation but not attributed.

## Past-batch comparison table

| Worker | Scenario set | B39 V | B41 V | B42 V | ΔvsB41 | Notes |
|---|---|---|---|---|---|---|
| W1 | chat_router_smoke | 2/7 | 2/7 | 3/7 | +1 | S6 pronoun R→V holds B40 v2 |
| W2 | stdlib_skills_core | 1/9 | 1/9 | 5/8 + 1B | +4* | *Rubric-only offline; events_pass=null |
| W3 | control_ir_ops | 3/9 | 3/9 | 5/9 | +2 | file_glob_grep + web_search new V |
| W4 | permissions_and_safety | 4/8 | 4/8 | 6/8 | +2 | S1 LLM-noise resolved |
| W5 | multi_agent_and_mcp | 1/7 | 0/7 | 0/7 | 0 | S6 mechanism shifted (flag for B43) |
| W6 | plan_mode + fp_0011 | varies | 1/11 | 0/7+1B | -1* | *Offline rubric + smaller scenario set |
| W7 | long_session_v1 | n/a | 2/7 | 2/7 | 0 | PR #221 verified on S7-T2 |
| **Total** | | **21/58** | **13/58** | **21/54+2B** | **+8\*** | *See caveats below |

## Caveats (= comparison_caveats from aggregate.json)

1. **W2 + W6 verdicts are offline rubric-only**. Batch crashed before workers wrote per-scenario results JSON; recovery sub-agents reconstructed verdicts from on-disk history.jsonl + scenario YAML rubric. Events gate was a hard pass/fail in B41; offline reconstruction sets `events_pass: null` and relies on rubric_pass alone. This INFLATES W2 ΔV=+4 (1→5). Real magnitude of the W2 improvement should be re-verified with a small events-gated re-run before claiming as B42 win.
2. **W6 scenario set differs from B41**: B41 W6 had 11 scenarios; B42 W6 had 7 (plan_mode 3 + fp_0011 subset 4). Per-worker ΔV is not apples-to-apples.
3. **W7 verdicts offline-aggregated**: driver ran cleanly, but `results-worker-7.json` was written manually post-crash. `events_pass` uses driver's empty-stop counter, not on-disk events.jsonl.

## Fix verification per PR

### PR #221 `describe_action` `_post_text` — **VERIFIED**

| | B41 (pre-fix) | B42 (post-fix) |
|---|---|---|
| W7-S7-T2 (= B41-NF-W7-1 target) | empty-stop | 1820 chars substantive answer |

Direct empirical improvement. Patch-isolation (= trace + revert) deferred but not needed — the size delta is overwhelming.

### PR #207 driver wait-for-skill-completion — **PARTIALLY VERIFIED**

- **Base case fixed**: W7 scenarios all ran 5 turns to completion (B41 truncated some at T2-T3 per W7 'notable' field).
- **Exposed-attractor**: W7-S2 pronoun-followup runs to completion → 4/5 turns empty-stop. Pre-PR-207 the driver truncated → "I" via "cannot judge"; post-PR-207 the empties are visible → "R". **This is a visibility win, not a regression.**
- **Uncovered case**: W6-S6 skill_builder invalid-spec still shows completion-event drop — distinct mechanism from spawn-ack timing that PR #207 fixed. PR #207 didn't promise to cover this path.

### PR #204 run_skill path resolution — **NOT EXERCISED**

No scenarios in this batch invoke `run_skill` with the `<name>/skill.md` short-form. W2-S7 (eval) routes correctly but trips on a downstream postprocessor schema bug (B41-NF-S7, separate). W5-S6 'tool not in catalog' message looks suspicious — flagged as NF-W5-B42-1 for B43 patch-isolation but NOT attributed to PR #204 without trace evidence.

## New findings for B43 (= carry-over)

| ID | Scenario | Hypothesis | Verify method |
|---|---|---|---|
| NF-W7-B42-1 | scenario_2_pronoun_followup | Mid-chain pronoun empty-stop attractor; B40 v2 ARS extend covers cold-start only | trace-patch-replay on S2 T2 |
| NF-W7-B42-2 | scenario_5_general_python_chain | T2 'plan tool not available' after T1 substantive — hot-list cache pollution? | dogfood_trace --mode llm-tools-schema on S5 T2 |
| NF-W7-B42-3 | scenario_6_file_and_doc_lookup_chain | Cold-start empty-stop NOT covered by B40 v2 — wrapper-description gap | trace-patch-replay on S6 T1 |
| NF-W5-B42-1 | mcp_install_permission_gate | PR #204 path resolution may have shifted skill__mcp_install dispatch mechanism | revert PR #204 + re-run S6 + event diff |
| NF-W6-B42-1 | plan_mode trio | Plan-step file_read consistently reports file-not-found / fabricates paths | trace single plan_mode scenario + inspect plan-step file events |
| NF-W6-B42-2 | s-fp11-1-builder-invalid-spec | skill_builder invalid-spec drops completion event (= distinct mechanism from PR #207 target) | events.jsonl trace through skill_builder validation-error path |

## Process notes

- **Crash recovery**: B42 main session crashed twice during A3 dispatch. 4 workers (W1/W3/W4/W5) wrote results before crash; W2/W6 had complete histories but no results JSON; W7 driver ran cleanly but had no aggregation. Recovery via 3 bounded sub-agents (offline-analyze W2 + W6, rerun-aggregate W7) restored full results without re-running ~$X in worker LLM calls. Recovery used `feedback_subagent_scope_bounding` discipline (= 1 deliverable / hard caps).
- **Methodology drift**: offline rubric-only verdicts (W2 + W6) are a known weakness of crash recovery. For B43, default to running workers to results-JSON-write completion before any context-budget management; or build a separate events-gated post-crash re-verification step.
- **No strong-model invocations**: all worker configs verified flash-lite per `feedback_no_strong_model`.

## Decision: B42 closure

B42 closes with V=21/54 (38.9%). Confirmed real-world ΔV ≈ +4 over B41 (= W3 + W4 events-gated wins). PR #204/207/221 are all KEEP — direct regression evidence absent, and PR #221 has a smoking-gun verified win. NF-W5-B42-1 should be the first B43 investigation item to rule out a quiet PR #204 regression.
