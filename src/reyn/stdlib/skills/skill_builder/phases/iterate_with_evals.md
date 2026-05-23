---
type: phase
name: iterate_with_evals
input: skill_builder_result
role: post_build_optimizer
can_finish: true
max_act_turns: 2
allowed_ops: [run_skill]
---

## Purpose

Optional post-build improvement loop. When the user requested iteration
(= ``data.max_iterations`` > 0), chain into ``skill_improver`` to run an
eval-driven loop on the just-built skill. When iteration was not
requested (= ``data.max_iterations == 0``, the default), pass the
build+lint result through unchanged.

This phase is the terminal of the ``skill_builder`` workflow. Output
is always a ``skill_builder_result`` — augmented with
``improvement_summary`` when iteration ran, untouched when it didn't.

## Decision tree

### Case 1 — No iteration requested (= ``data.max_iterations == 0``)

Emit a decide turn that finishes with the input artifact verbatim. No
ops, no run_skill, nothing. The skill_builder workflow ends with the
build+lint result as before.

```
control.type = "finish"
control.decision = "finish"
control.next_phase = null
artifact = data (passthrough)
```

This is the legacy behaviour for callers that don't opt into iteration.

### Case 2 — Iteration requested (= ``data.max_iterations >= 1``)

Run one ``run_skill`` op invoking ``skill_improver`` on the
just-built skill. ``skill_improver`` will:

  1. Look for an existing ``eval.md`` under the skill directory.
  2. If absent, auto-generate one via ``eval_builder``.
  3. Loop: ``run_and_eval`` → ``plan_improvements`` → ``apply_improvements``
     until score threshold is met, max iterations reached, regression /
     stagnation detected, or no productive changes remain.

Op shape:

```
{
  "kind": "run_skill",
  "skill": "skill_improver",
  "input": {
    "type": "improvement_session",
    "data": {
      "target_skill_path": "<data.skill_path>/skill.md",
      "max_iterations": <data.max_iterations>,
      "score_threshold": <data.score_threshold>,
      "improvement_focus": ""
    }
  }
}
```

The op returns ``final_output`` matching ``improvement_result``:
``score_history``, ``files_modified``, ``termination_reason``, etc.

### Case 2 decide turn

After the run_skill op returns, emit a decide turn finishing with a
``skill_builder_result`` artifact built by:

  - Passing through all build+lint fields from the input
    (``skill_name``, ``skill_path``, ``files_written``, ``file_count``,
    ``lint_passed``, ``lint_issues``, ``summary``).
  - Setting ``max_iterations`` and ``score_threshold`` from input.
  - Setting ``improvement_summary`` from the run_skill result:
    ```yaml
    improvement_summary:
      iterations_run: <improvement_result.iterations_run or len(score_history)>
      initial_score: <score_history[0] if non-empty else 0.0>
      final_score: <score_history[-1] if non-empty else 0.0>
      termination_reason: <improvement_result.termination_reason>
      files_modified: <improvement_result.files_modified>
    ```
  - Augmenting ``summary`` with a brief tail like
    " (improved across <iterations_run> iteration(s), final score
    <final_score>, ended <termination_reason>)" so the user-facing
    summary reflects what happened.

### Case 3 — run_skill error

If the ``run_skill`` op returns ``status: error``, finish with a
``skill_builder_result`` carrying the original build+lint data and
``improvement_summary.termination_reason = "error"`` plus the error
text in ``improvement_summary.files_modified[0]`` as a diagnostic
note. Do NOT abort the whole workflow — the build itself succeeded
and the user should still see those files exist.

## Constraints

- Exactly 0 or 1 act turns. Never more than one ``run_skill`` op.
- Do NOT modify any file. ``skill_improver`` owns that responsibility.
- Do NOT call lint here. ``verify_skill`` already linted; if iteration
  modifies the skill, ``skill_improver`` re-validates internally.

## Why this lives in skill_builder

Putting the iteration here (rather than asking the user to type
``reyn run skill_improver <name>`` afterward) makes ``skill_builder``
a complete "build a high-quality skill from one command" tool. The
opt-in default (= ``max_iterations: 0``) preserves backward compat:
existing callers that didn't ask for iteration still get the fast
build→lint→finish path.
