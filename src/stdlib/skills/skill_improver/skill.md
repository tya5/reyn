---
type: skill
name: skill_improver
description: Iteratively improve an existing skill by running it under eval, planning DSL changes against the failing criteria, applying them, and re-evaluating until a score threshold is met or a stop condition fires.
entry: prepare
final_output: improvement_result
final_output_description: |
  Outcome of the improver loop — score progression, files modified, and the
  reason the loop terminated (threshold met, max iterations, regression, etc.).
finish_criteria:
  - The DSL changes from at least one iteration have been written to disk
  - improvement_result records the score history and a termination reason
  - The user has a concrete next-step command to verify the improvement
graph:
  prepare: [run_and_eval]
  run_and_eval: [plan_improvements]
  plan_improvements: [apply_improvements]
  apply_improvements: []
---

## Overview

Runs the target skill under an eval, plans concrete DSL changes against the failing criteria, applies them in place, and re-evaluates — repeating until a score threshold is met or a stop condition fires. `skill_improver` invokes the `eval` and `eval_builder` skills via the `run_skill` Control IR op (no `--allow-shell` needed).

## Phase flow

```
prepare  →  run_and_eval  →  plan_improvements  →  apply_improvements
                  ↑___________________________________|
                       (rollback for next iteration)
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `prepare` | coordinator | Parses the request, ensures an `eval.md` exists (auto-generates via `eval_builder` if not), picks a test case, initializes session state |
| `run_and_eval` | evaluator | Invokes the `eval` stdlib skill via `run_skill`; records the score in `iteration_state` |
| `plan_improvements` | architect | Reads the target's DSL files, diagnoses the weakest phase, and proposes minimal DSL changes targeting failing criteria. Adapts strategy from iteration history (regression / stagnation detection) |
| `apply_improvements` | implementer | Writes the proposed changes to disk, persists iteration state to `improver_state.json`, then either **finishes** or **rolls back** to `run_and_eval` for iteration N+1 |

The loop is realized via OS rollback: when `apply_improvements` rolls back, the next visit to `run_and_eval` reads the just-modified DSL on disk and starts a fresh iteration.

## Loop termination (defense in depth)

`apply_improvements` finishes when **any** of these conditions holds (first match wins):

1. `score >= score_threshold` (default 0.85) — the target is good enough.
2. `iteration >= max_iterations` (default 3) — hard cap reached.
3. `changes` array is empty — `plan_improvements` signaled "no more useful changes".
4. **Regression**: latest score < previous iteration's score — last change made things worse; the change is reverted and the loop exits.
5. **Stagnation**: |latest − previous| < 0.02 (and iteration > 1) — strategy not progressing.

The OS-level `max_phase_visits` cap (default 25) is the final safety net for any chain that gets stuck.

## Input

Either natural language (auto-wrapped to `user_message`) or a structured `improvement_session` JSON.

```
reyn run skill_improver "Improve reyn/local/my_skill, using its eval.md, max 3 iterations, threshold 0.9"
```

```
reyn run skill_improver '{
  "type": "improvement_session",
  "data": {
    "target_skill_path": "reyn/local/my_skill/skill.md",
    "max_iterations": 3,
    "score_threshold": 0.85,
    "improvement_focus": "review phase rejection logic"
  }
}'
```

If no `eval.md` exists at `<target_dsl_root>/eval.md`, `prepare` invokes `eval_builder` to generate one before the loop starts. If `target_skill_path` is missing, `prepare` asks for it via `ask_user`.

## Output

`improvement_result` summarizes the score progression, the union of files modified across iterations, and the termination reason. `termination_reason` is one of:

| Value | Meaning |
|-------|---------|
| `score_threshold_met` | Reached the configured threshold |
| `max_iterations_reached` | Hit the iteration cap |
| `regression_detected` | Score went down — change reverted, loop stops |
| `stagnation_detected` | Two consecutive iterations with no score change |
| `no_more_changes_planned` | `plan_improvements` could not find a productive next change |

Patches are applied **in place** to the target skill's DSL files — there is no separate `dsl_patches/` staging directory. Inspect `improver_state.json` in the project root for full iteration history. Re-run `reyn eval <eval_spec_path>` to independently confirm the improvement.
