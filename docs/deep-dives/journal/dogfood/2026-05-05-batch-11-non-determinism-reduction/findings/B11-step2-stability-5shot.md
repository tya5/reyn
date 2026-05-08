# B11 Step 2 — 5-shot stability retest

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `2d892e6` |
| Fixes active | R1 + R3 + R2 |
| Sample size | N=5 |
| Complete rate | 0/5 (0%) |
| Routing-fail rate | 3/5 (60%) |
| Partial rate | 2/5 (40%) |
| LiteLLM model | `openai/gemini-2.5-flash-lite` |
| Total cost | ~$0.0023 USD |

## Per-session verdicts

<table>
<thead>
<tr>
<th>Session</th>
<th>Verdict</th>
<th>Router action</th>
<th>Furthest phase reached</th>
<th>Stopping reason</th>
<th>Cost</th>
</tr>
</thead>
<tbody>
<tr>
<td>Run 1</td>
<td><strong>partial</strong></td>
<td>invoke_skill directly (R3 fix effective)</td>
<td>prepare → copy_to_work (start)</td>
<td>copy_to_work preprocessor step[1] file.read permission_denied (same as B8-NEW-1)</td>
<td>$0.0005</td>
</tr>
<tr>
<td>Run 2</td>
<td><strong>routing-fail</strong></td>
<td>text-reply (stop, no tool call)</td>
<td>N/A — no workflow started</td>
<td>Router asked clarifying question instead of invoking skill</td>
<td>$0.0003</td>
</tr>
<tr>
<td>Run 3</td>
<td><strong>routing-fail</strong></td>
<td>text-reply (stop, no tool call)</td>
<td>N/A — no workflow started</td>
<td>Router replied with clarification text about skill_improver / direct_llm</td>
<td>$0.0003</td>
</tr>
<tr>
<td>Run 4</td>
<td><strong>partial</strong></td>
<td>invoke_skill directly (R3 fix effective, 3 attempts)</td>
<td>prepare → copy_to_work (start), 3x</td>
<td>copy_to_work preprocessor step[1] file.read permission_denied (all 3 attempts)</td>
<td>$0.0010</td>
</tr>
<tr>
<td>Run 5</td>
<td><strong>routing-fail</strong></td>
<td>text-reply (stop, no tool call)</td>
<td>N/A — no workflow started</td>
<td>Router asked about improvement criteria</td>
<td>$0.0002</td>
</tr>
</tbody>
</table>

## Aggregated metrics

| Metric | Value |
|---|---|
| complete (6-phase full run) | 0/5 (0%) |
| partial (past prepare, stopped before finalize) | 2/5 (40%) |
| routing-fail (no skill invoked) | 3/5 (60%) |
| Most common stopping point | copy_to_work preprocessor step[1] (all partials) |
| Routing-fail pattern | text-reply (clarification request), not empty-stop |

### R3 fix (router direct invoke) observed effect

- Sessions where R3 fix fired correctly: 2/5 (runs 1 and 4)
- Sessions where routing-fail persisted: 3/5 (runs 2, 3, 5)
- Routing-fail is still text-reply (R3 pre-fix rate was 50-60% text-reply; observed 60%)
- R3 fix reduced routing-fail in ~40% of cases but not reliably

### R2 fix (describe_skill routing strip) observed effect

- Sessions with invoke_skill (runs 1 and 4): no G12 attractor observed
- The routing strip appears to prevent the empty-stop attractor in sessions that reach invoke_skill
- Cannot distinguish R2 contribution from R3 in successful-routing sessions

### R1 fix (\_resolved\_paths schema) observed effect

- In all partial sessions (runs 1 and 4): copy_to_work preprocessor step[0] (python compute_paths) succeeded
- Blocking issue is step[1] (run_op file read), not path hallucination
- R1 fix appears effective but cannot confirm without reaching run_and_eval

## Delta vs batch 10 (50% baseline)

| Metric | Batch 10 (N=2) | Batch 11 Step 2 (N=5) | Delta |
|---|---|---|---|
| complete rate | 50% (1/2) | 0% (0/5) | -50pp |
| routing-fail rate | 50% (1/2) | 60% (3/5) | +10pp |
| partial rate | 0% (0/2) | 40% (2/5) | +40pp |

**Interpretation**: Batch 10's 50% complete rate was achieved with a different blocking
structure (copy_to_work was not blocked then). In this run, copy_to_work's
`file.read permission_denied` (B8-NEW-1) is a hard blocker for all sessions that
reach the chain. The R3 fix improved routing in 40% of sessions but 60% routing-fail
persists. The pre-fix baseline was 50-60% routing-fail; observed 60% — marginal or no
improvement in practice.

**Key finding**: The dominant blocker is now **copy_to_work file.read permission_denied
(B8-NEW-1)**, not routing non-determinism. Even when R3 correctly dispatches the skill,
the chain terminates at copy_to_work. R3 fix contribution is necessary but not sufficient.

## New bugs observed

### B11-NEW-1: copy_to_work file.read permission_denied blocks every partial run

This bug was first observed in B8-S1 fresh retest as B8-NEW-1. It is now confirmed as
the primary blocker in batch 11 Step 2 integration testing.

**Error**: `Phase 'copy_to_work' preprocessor step[1] run_op (file): read from
'<worktree>/src/reyn/stdlib/skills/direct_llm/skill.md' was not approved.`

All 2 partial sessions (runs 1 and 4) stopped at exactly this point. The fix would be:
declare the stdlib skills path glob in `skill_improver`'s permissions frontmatter, or
have the OS auto-approve `run_op` preprocessor file reads to paths already visible in
the skill's declared path list.

### B11-NEW-2: R3 router fix is non-deterministic (60% text-reply persists)

Despite the structural rule change ("If the user names a skill that appears in the
Available skills list, call invoke_skill directly"), 3/5 sessions still produced
text-reply routing-fail. The fix is partially effective (pre-fix: ~60%, post-fix: 60%)
with no statistically significant improvement in this N=5 sample.

Possible root causes:
- The LLM does not consistently recognize `skill_improver` in the Available skills list
- The new rule text is not consistently weight-dominant over the LLM's clarification-seeking behavior
- The Available skills list injection may vary by session context

## Verdict vs prelude prediction

| Prediction | Actual |
|---|---|
| 60-70% complete rate (3-4/5) | 0% complete (0/5) |
| Routing-fail improvement to ~10% | 60% routing-fail |
| R3 fix verified | Partially (2/5 sessions) |

**Prediction refuted.** The prelude predicted 60-70% complete rate based on R1+R2+R3
fixes. Actual: 0% complete due to two compounding issues:
1. copy_to_work file.read permission_denied (B8-NEW-1, known but not fixed) blocks all
   partial sessions
2. R3 routing fix is non-deterministic — 60% text-reply persists

The batch 10 "50% complete" baseline was achieved without the copy_to_work blocker being
the gating issue in those specific runs. The current state is regressed on complete rate
relative to batch 10, though routing in successful sessions (runs 1, 4) shows R3 and R2
improvements working.
