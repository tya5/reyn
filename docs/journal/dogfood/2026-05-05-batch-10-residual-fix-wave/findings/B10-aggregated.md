# B10 Step 3 — Integration Retest Aggregated Summary

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `21c1497` |
| Test suite | 1005 passed / 2 xfailed |
| Batch | 10 (B9-NEW residual fix wave) |
| Step | 3 — integration retest |

## Scenario Results

| Scenario | Input | B9 Verdict | B10 Verdict | Top Observation |
|---|---|---|---|---|
| **S1** (skill_improver chain) | `skill_improver で direct_llm を 1 回 review して改善案を出して` | inconclusive | **verified** | First full chain completion via `reyn chat` — all 6 phases + sub-skills |
| **S5a** (natural lang eval_builder) | `direct_llm の eval を作って` | refuted | **refuted** | Router text-reply; G16 still unresolved |
| **S5b** (structured eval_builder) | `eval_builder で direct_llm を analyze して、 target_skill=direct_llm` | refuted | **refuted** | G12 attractor (stop_with_must_rule) — non-deterministic; B9-NEW-2 fix is structurally sound |

## Dogfood Milestone Confirmation

**YES — First chain completion via `reyn chat`.**

S1 Run 2 produced the first fully observed `reyn chat` session where `skill_improver`
ran all phases to `finalize`:

```
prepare → copy_to_work → run_and_eval → plan_improvements → apply_improvements → finalize
```

Sub-skills also completed: `eval_builder` (analyze_skill → write_eval) and `eval` (×2).
Narrator ran. Improvement plan was delivered.

Prior best: B9-S1 inconclusive (chain progressed significantly but finalize not confirmed).
B8-S1 and earlier: blocked (router failures or copy_to_work permission stop).

## S1 Chain Detail

```
[T+2.0s]  router invoke → skill_improver
[T+3.0s]  run_skill(eval_builder) → analyze_skill → write_eval (eval.md written)
[T+20.0s] copy_to_work completed
[T+22.0s] run_skill(eval) → run_target → evaluate
[T+30.0s] plan_improvements completed
[T+37.0s] apply_improvements completed (1 phase_retry, recovered)
[T+39.0s] finalize completed
[T+39.0s] skill_narrator ran (narrate → finished)
Total wall time: ~60s
```

## Cost Summary (3 sessions combined)

| Session | Calls | Tokens | Cost |
|---|---|---|---|
| S1 Run 1 (router fail) | 1 | 2,315 | $0.000250 |
| S1 Run 2 (decisive) | 25 | 149,262 | $0.000548 |
| S5a | 1 | 2,336 | $0.000269 |
| S5b | 4 | 11,835 | $0.001197 |
| **Total** | **31** | **165,748** | **$0.002264** |

Note: Majority of tokens (144k in S1 Run 2) are cached/no-charge on the LiteLLM proxy;
real billed cost is ~$0.000548 for S1 Run 2.

## New Bugs (B10-NEW)

| ID | Scenario | Observation | Severity | Batch 11? |
|---|---|---|---|---|
| **B10-NEW-1** | S1 | `run_skill` to temp workspace path fails (`/tmp/reyn-workspace/skills/direct_llm` not found) during eval.run_target. Phase retried and chain continued — non-blocking. | MED | YES |
| **B10-NEW-2** | S1 | Router non-determinism: Run 1 text-reply (B9-NEW-3 not fixed), Run 2 clean invoke. B9-NEW-3 pattern persists. | MED | YES |

## Unresolved from Prior Batches

| Bug | Status | Note |
|---|---|---|
| G16 (natural-lang eval_builder routing) | unresolved | S5a refuted again — router clarification-ask pattern |
| G12 (stop_with_must_rule attractor) | unresolved | S5b hit attractor; probabilistic ~25% per session |
| B9-NEW-3 (router text-reply instead of invoke) | unresolved | S1 Run 1 confirmed; Run 2 bypassed non-deterministically |

## Batch 11 Candidates

Priority order:

1. **B10-NEW-1** (HIGH) — temp workspace path bug: `run_skill` from `eval.run_target` uses
   `/tmp/reyn-workspace/...` or `/tmp/reyn_workspace/...` paths that don't exist. The path
   naming differs between the two eval cycles in S1 (one uses hyphen, one uses underscore).
   Chain survives via retry but eval runs on degraded data.

2. **G12** (MED) — stop_with_must_rule attractor root cause. The two contradicting MUST rules
   in the router system prompt produce completion_tokens=0 responses after describe_skill.
   Probabilistic but blocks S5b ~25-50% of the time.

3. **B9-NEW-3 / B10-NEW-2** (MED) — router text-reply instead of invoke. Non-deterministic.
   Requires 2 attempts for S1 reliability. Root cause in router intent classification.

4. **G16** (LOW) — natural-language routing to eval_builder. Router identifies the skill
   but classifies intent as "Reply" rather than "Action" for indirect phrasings.

## Calibration Assessment

| Prediction | Actual | Assessment |
|---|---|---|
| S1: 30-40% verified | verified | Calibration correct — within range |
| S5a: refuted (continuing pattern) | refuted | Correctly anticipated |
| S5b: predicted verified (Step 1 showed fix effective) | refuted | Miss — G12 attractor is non-deterministic, Step 1 happened to avoid it |

Lesson: Step 1 verified the B9-NEW-2 fix path but did not predict the G12 attractor
probability. When two independent issues affect a scenario, fixing one may not change
the overall success rate if the other remains.
