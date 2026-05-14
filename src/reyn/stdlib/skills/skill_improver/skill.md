---
type: skill
name: skill_improver
description: Iteratively improve an existing skill by working on a temp copy, running eval, planning DSL changes, applying them, and re-evaluating until a score threshold is met. Only copies changes back to the original on success.
entry: prepare
final_output: improvement_result
final_output_description: |
  Outcome of the improver loop — score progression, files modified, and the
  reason the loop terminated (threshold met, max iterations, regression, etc.).
finish_criteria:
  - The improvement loop has run at least one iteration and terminated
  - improvement_result records the score history and a termination reason
  - If score_threshold_met, improved files have been copied back to the original skill directory
  - The user has a concrete next-step command to verify the result
graph:
  prepare: [copy_to_work]
  copy_to_work: [run_and_eval]
  run_and_eval: [plan_improvements]
  plan_improvements: [apply_improvements]
  apply_improvements: [finalize]
  finalize: []
routing:
  intents: [task]
  when_to_use:
    - User wants to improve / refine / iterate on an existing skill
    - User mentions raising eval score or fixing failing criteria
  when_not_to_use:
    - User wants to build a fresh skill (use skill_builder)
    - User wants only to *evaluate* without modifying (use eval)
    - User wants to generate eval criteria (use eval_builder)
  examples:
    positive:
      - "skill X を改善して"
      - "eval の点数を上げたい"
      - "失敗してる criteria を直して"
    negative:
      - "skill X を eval して"   # this is eval, not improver
      - "新しい skill を作って"   # this is skill_builder, not improver
permissions:
  # copy_to_work reads skill DSL files from the resolved skill directory.
  # Stdlib skills live under src/reyn/stdlib/skills/ which may be outside
  # the project root when running from a worktree (B8-NEW-1). Declare
  # recursive read access for all three skill search paths so startup_guard
  # prompts once and saves approval for the run.
  file.read:
    - path: src/reyn/stdlib/skills
      scope: recursive
    - path: reyn/local
      scope: recursive
    - path: reyn/project
      scope: recursive
  python:
    - module: ./copy_to_work.py
      function: extract_skill_name
      mode: safe
      timeout: 5
    - module: ./copy_to_work_resolver.py
      function: resolve_paths
      mode: unsafe
      timeout: 5
    - module: ./copy_to_work.py
      function: build_copy_plan
      mode: safe
      timeout: 5
    - module: ./copy_to_work.py
      function: build_write_ops
      mode: safe
      timeout: 5
    - module: ./copy_to_work.py
      function: validate_copy
      mode: safe
      timeout: 5
    - module: ./copy_to_work.py
      function: inject_resolved_paths
      mode: safe
      timeout: 5
---

## Overview

Copies the target skill to a temp work directory (`.reyn/skill_improver_work/<name>/`), then iteratively improves it: runs eval, plans DSL changes, applies them, and re-evaluates. On success (`score_threshold_met`), copies the improved files back to the original location. On any other stop condition (regression, stagnation, cap reached), the original skill is left untouched.

`skill_improver` invokes the `eval` and `eval_builder` skills via the `run_skill` Control IR op (no `--allow-shell` needed).

## Phase flow

```
prepare  →  copy_to_work  →  run_and_eval  →  plan_improvements  →  apply_improvements  →  finalize
                                    ↑___________________________________|
                                           (rollback for next iteration)
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `prepare` | coordinator | Parses the request, ensures an `eval.md` exists (auto-generates via `eval_builder` if not), picks a test case, initializes session state |
| `copy_to_work` | workspace_initializer | Globs the target skill's DSL files, copies them to `.reyn/skill_improver_work/<name>/`, and updates `target_skill_root` in the session to the temp path |
| `run_and_eval` | evaluator | Invokes the `eval` stdlib skill via `run_skill`; records the score in `iteration_state` |
| `plan_improvements` | architect | Reads the target's DSL files from the work dir, diagnoses the weakest phase, and proposes minimal DSL changes targeting failing criteria. Adapts strategy from iteration history (regression / stagnation detection) |
| `apply_improvements` | implementer | Writes the proposed changes to the work dir, persists iteration state to `.reyn/improver_state.json`, then either **transitions to finalize** or **rolls back** to `run_and_eval` for iteration N+1 |
| `finalize` | finalizer | On `score_threshold_met`: copies improved files from work dir back to the original skill directory. On any other stop: leaves the original untouched and reports the work dir location for inspection |

The loop is realized via OS rollback: when `apply_improvements` rolls back, the next visit to `run_and_eval` reads the just-modified DSL from the work dir and starts a fresh iteration.

## Loop termination (defense in depth)

`apply_improvements` hands off to `finalize` when **any** of these conditions holds (first match wins):

1. `score >= score_threshold` (default 0.85) — the target is good enough → `finalize` copies files back.
2. `iteration >= max_iterations` (default 3) — hard cap reached → `finalize` skips copy-back.
3. `changes` array is empty — `plan_improvements` signaled "no more useful changes" → `finalize` skips copy-back.
4. **Regression**: latest score < previous iteration's score → `finalize` skips copy-back; original skill is unmodified.
5. **Stagnation**: |latest − previous| < 0.02 (and iteration > 1) → `finalize` skips copy-back.

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

If no `eval.md` exists at `<target_skill_root>/eval.md`, `prepare` invokes `eval_builder` to generate one before the loop starts. If `target_skill_path` is missing, `prepare` asks for it via `ask_user`.

## Output

`improvement_result` summarizes the score progression, the union of files modified across iterations, and the termination reason. `termination_reason` is one of:

| Value | Meaning |
|-------|---------|
| `score_threshold_met` | Reached the configured threshold |
| `max_iterations_reached` | Hit the iteration cap |
| `regression_detected` | Score went down — change reverted, loop stops |
| `stagnation_detected` | Two consecutive iterations with no score change |
| `no_more_changes_planned` | `plan_improvements` could not find a productive next change |

Changes are applied to a **temp work directory** (`.reyn/skill_improver_work/<name>/`) and only written back to the original skill when `score_threshold_met`. Inspect `.reyn/improver_state.json` for full iteration history and `.reyn/skill_improver_work/<name>/` for the improved DSL files. Re-run `reyn eval <eval_spec_path>` to independently confirm the improvement.
