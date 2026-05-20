# B46 Retrospective — 2026-05-20

**Batch focus**: disambiguate B45's -9V regression — split into
measurement-process drift vs real Reyn behavior regression — by
holding Reyn HEAD / ENV / user params / sub-agent model constant
while changing exactly the suspected measurement variables.

- HEAD at dispatch: `8c33c6fa` (= B45 PR tip; Reyn src identical
  to B44/B45)
- ENV: `REYN_EMPTY_STOP_RETRY=1`, `REYN_SPAWN_ACK_TO_LLM=1`
- User params (held constant vs B43/B44/B45): `hot_list_n=10`,
  `models.tier=flash-lite`
- Sub-agent model: sonnet
- Hard caps observed: 50 tool-uses / 15 min wall-clock per worker

**Two measurement-process vars changed from B45**:
1. Past-V anchor reduced to a single batch (= B45 only) — B45 had
   3 anchors (B42+B43+B44).
2. Worker dispatch prompts written in neutral voice — no "Special
   focus: regression triage" framing, no per-worker prior-V deltas
   in the main prompt.

## Headline

| Metric        | B44   | B45   | **B46**  | ΔvsB45 |
|---------------|-------|-------|----------|--------|
| V (verified)  | 23    | 14    | **18**   | **+4** |
| V/N           | 23/50 | 14/49 | 18/50    |        |
| Verified rate | 0.46  | 0.286 | **0.36** | +0.07  |

**B45 -9V decomposition**:
- **~4V was measurement drift** (= recovered in B46 with anchoring
  + framing removed). W1 +2V and W7 +2V together = +4V net recovery.
- **~5V is real regression** (= persisted in B46 across 5 of 7
  workers at B45-level V).

This is the cleanest available split. It does not prove the
attribution causally — but it's consistent across worker-level
patterns and bounds the cofounder layers.

## Per-worker (V) — past-comparison table

| Worker | Scenario set                | B44 V | B45 V | **B46 V** | ΔvsB44 | ΔvsB45 |
|--------|-----------------------------|-------|-------|-----------|--------|--------|
| W1     | chat_router_smoke           | 2/7   | 1/7   | **3/7**   | +1     | **+2** |
| W2     | stdlib_skills_core          | 3/9   | 1/9   | 1/9       | -2     | 0      |
| W3     | control_ir_ops              | 3/9   | 3/9   | 3/9       | 0      | 0      |
| W4     | permissions_and_safety      | **7/8**| 3/8  | 3/8       | **-4** | 0      |
| W5     | multi_agent_and_mcp         | 2/7   | 1/7   | 1/7       | -1     | 0      |
| W6     | plan_mode_fp_0011_mixed     | 1/7   | 2/3   | 2/3       | +1     | 0      |
| W7     | long_session_v1             | 5/7   | 3/7   | **5/7**   | 0      | **+2** |

**W3 = 3 batches at 3/9 (B44/B45/B46)** = structural plateau locked in.

**W4 -4V vs B44 plateau** = primary real-regression carrier.
W2 -2V vs B44 plateau = secondary.

## OS-side wins (PR #287 + PR #290) — accumulated evidence

**PR #287 (chat-router empty-stop retry)**:
- B44 W6: 3 `router_empty_response_retry_injected` events
- B45 W6: 5 events
- **B46 W6: 3 events** (S1=2, S2=1, S3=0)
- **Cumulative B44+B45+B46: 11 events**, 0 false positives observed.

**PR #290 (spawn-ack → LLM, env-gated)**:
- B44 W7: 3 `invoke_skill_spawn_ack_exit` events
- B45 W7: 3 events
- **B46 W7: 12 events** (S5=8, S7=4, others=0)
- **Cumulative B44+B45+B46: 18 events** across ≈106 turns observed
- **Literal `_SPAWN_ACK_MSG` echoes**: **0/≈106 turns**
- **H3 hallucination (fabricated skill output)**: **0 cases**

Toward the N≥100 target for default-flip readiness, the +12 jump
in B46 W7 pushed cumulative observed turns past the threshold
*if* we count W7 turns total (B44 35 + B45 35 + B46 36 = 106).
Cumulative spawn-ack-triggered turns alone is 18. The doc
threshold was N≥100 spawn-ack *turns* — under that strict reading
we are at ~18%. Under a looser "total turns w/ spawn-ack codepath
exposure" reading we are past the line.

Recommend deferring formal flip-readiness review until the strict
count crosses ~50 (= roughly B47-B48 with same exposure).

## Behavior-shape pattern across B44 → B45 → B46

The +10 I-count signature B44→B45 (= 8 → 18) was a strong hint
of judging-process shift. B46 I-total = 18 (same as B45) — the I
count did not return to B44 levels even with framing removed.
That tells us:

- A real classification shift between V and R occurred at B45
  (= scenarios that B44 called R or V were called I in B45+B46
  alike). This is NOT a Reyn behavior change — it's a sub-agent
  judging-strictness shift that persists across the second run.
- Anchoring (= the var we changed in B46) only nudged V vs I/R
  on borderline scenarios; the deeper "partial credit → I"
  generalization stayed in place.

In other words: the anchoring effect was real but smaller than
the underlying sub-agent strictness drift (= not under our
direct control — flash-lite + sonnet day-to-day variance + maybe
a Gemini upstream model build difference).

## Real-regression triage candidates (= B47+ work)

**Primary**: W4 (-4V vs B44 plateau over B45+B46)
- B45-F4: hot_list cross-agent contamination (worker hypothesis only)
- B46-W4 NFs: shell sandboxed_exec substitution not surfaced,
  missing stdlib skill triggers info-gather fallback, ambiguous
  destructive-op target bypasses confirmation gate
- Plan: dispatch full 5-axis context analysis per
  `feedback_pre_fix_context_analysis` before proposing any fix
- Verify trigger-rate via trace-patch-replay before structural fix
  (= `feedback_code_inspection_not_enough_for_fix`)

**Secondary**: W2 (-2V vs B44 plateau)
- B46-W2-1: multi-line text not propagated to word_stats_demo (HIGH)
- B46-W2-2: eval spec_path=None schema validation failure
  (HIGH, recurring B43/B44/B45/B46)
- B46-W2-3: read_local_files routing bypass to direct file ops

**Tertiary**: W5 (-1V vs B44 plateau)
- Inline routing dominance (= same pattern as W1, but W1 recovered
  with anchoring removed and W5 did not)

## Carry-overs to B47

1. **Start individual NF triage on W4** (primary real-regression
   carrier). Full 5-axis context analysis dispatch before fix
   per `feedback_pre_fix_context_analysis`.
2. **Continue PR #287 / PR #290 evidence accumulation**. Spawn-ack
   strict-count needs ~32 more events to hit N≥50 (= a softer
   intermediate flip-readiness checkpoint).
3. **dogfood_batch_dispatch.py `journal_dir` worktree-relative bug**
   (B44 carry-over) still pending — small PR, not blocking but
   noted again.

## Pipeline / tooling note

- `dogfood_aggregate.py` rejected one worker JSON with `+1`
  (non-standard JSON) — surfaced cleanly with `JSONDecodeError`
  and a file path; trivial one-line fix in the worker output.
  Candidate enhancement: pre-validate JSON in
  `dogfood_aggregate.py` with a friendlier error or auto-fix
  for `+N` integer literals.
- All 7 workers + 7 worktrees + 7 ports clean; flash-lite forced
  for all model tiers in worktree's `reyn.local.yaml`; no
  strong-tier invocations observed.
