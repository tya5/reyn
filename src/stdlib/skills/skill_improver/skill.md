---
type: skill
name: skill_improver
description: Iteratively improve an existing app by running it under eval, planning DSL changes against the failing criteria, applying them, and re-evaluating until a score threshold is met or a stop condition fires.
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

`app_improver` runs an improvement loop on a target app:

1. **prepare** — parses the user's request, ensures an `eval.md` spec exists for the target (auto-generates one via `eval_builder` if missing), picks a test case, and initializes workspace state.
2. **run_and_eval** — invokes the `eval` stdlib sub-app to score the target against the chosen case's per-phase criteria. Records the score in `iteration_state`.
3. **plan_improvements** — reads the target app's DSL files, diagnoses the weakest phase, and proposes minimal DSL changes targeting the failing criteria. Adapts strategy based on iteration history (regression/stagnation detection).
4. **apply_improvements** — writes the proposed changes to disk, commits the iteration to `improver_state.json`, and decides whether to **finish** or **rollback** for another iteration.

The loop is realized via OS rollback: when `apply_improvements` rolls back, the chain returns through `plan_improvements` to `run_and_eval`, which reads the updated workspace state and starts iteration N+1 against the just-modified DSL.

## Loop termination (defense in depth)

`apply_improvements` finishes when **any** of these conditions holds (first match wins):

1. `score >= score_threshold` (default 0.85) — the target is good enough.
2. `iteration >= max_iterations` (default 3) — hard cap reached.
3. `changes` array is empty — plan_improvements signaled "no more useful changes".
4. **Regression**: latest score < previous iteration's score — last change made things worse.
5. **Stagnation**: |latest − previous| < 0.02 (and iteration > 1) — strategy not progressing.

The OS-level `max_phase_visits` cap (default 25) is the final safety net for any chain that gets stuck.

## Input

Either natural language (auto-wrapped to `user_message`) or a structured `improvement_session` JSON.

```
reyn run skill_improver "Improve reyn/local/my_app, using its eval.md, max 3 iterations, threshold 0.9"
```

```
reyn run skill_improver '{
  "type": "improvement_session",
  "data": {
    "target_skill_path": "reyn/local/my_app/skill.md",
    "max_iterations": 3,
    "score_threshold": 0.85,
    "improvement_focus": "review phase rejection logic"
  }
}'
```

If no `eval.md` exists at `<target_dsl_root>/eval.md`, prepare invokes `eval_builder` to generate one before the loop starts.

## Output

`improvement_result` summarizes the score progression, files modified across iterations, and the termination reason. Re-run `reyn eval <eval_spec_path>` to independently confirm.
