---
type: phase
name: apply_improvements
input: improvement_plan
role: implementer
model_class: standard
max_act_turns: 1
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files]
---

Commit the iteration to history, optionally apply the planned DSL changes, then decide whether to loop or finish.

**CRITICAL — single act turn**: This phase allows exactly **1 act turn** (`max_act_turns: 1`). Issue ALL file ops (Steps 1 and 2 combined) in that one act turn. If you exceed the budget, the OS forces a decide turn immediately — you lose the ability to issue further ops. Issue Steps 1 and 2 ops together in the SINGLE act turn. After receiving results, your next response MUST be a decide turn — do NOT issue any more ops.

This phase uses exactly **1 act turn** followed by **1 decide turn**:
- **Act turn** — issue ALL file ops (DSL changes + `.reyn/improver_state.json` read/write) in a single act turn.
- **Decide turn** — receive results, then emit `improvement_result` or issue rollback. Do NOT emit any more ops.

## Step 1 — Apply DSL changes (conditional)

If `improvement_plan.changes` is non-empty, for each entry, issue:
- `create` or `update` → file write op where:
  - `path` = `change.file` (verbatim)
  - `content` = `change.new_content` (verbatim — never substitute, never invent)
- `delete` → file delete op with `path = change.file`

File paths are project-relative.

ABSOLUTE RULES:
- NEVER write a file whose path is not listed in `improvement_plan.changes[*].file`.
- NEVER write `session.case_input`, `eval_spec_path`, or any other iteration_state field as file content.
- Empty `changes` means: emit zero file write/delete ops in this step.

Track the resulting paths as `applied_files` for use in Step 2. Empty list when `changes` was empty.

## Step 2 — Commit the iteration to history (MANDATORY EVERY VISIT)

This step ALWAYS runs, regardless of whether Step 1 produced any file ops. Skipping it breaks the loop.

In the SAME first act turn (alongside any Step 1 ops), issue:

A. ONE file read op for `.reyn/improver_state.json`.
B. ONE file write op for `.reyn/improver_state.json` whose `content` is the JSON-serialized updated state, computed as:

```
existing_state = <result of the file read>
new_entry = {
  "iteration":      improvement_plan.iteration_state.current_iteration,
  "eval_score":     improvement_plan.iteration_state.latest_eval.overall_score,
  "weakest_phase":  improvement_plan.iteration_state.latest_eval.weakest_phase,
  "files_modified": applied_files,
  "plan_summary":   improvement_plan.summary
}
updated_state = {
  "session":    existing_state.session,
  "iterations": existing_state.iterations + [new_entry]
}
```

The runtime returns ops results in order, so include both A and B in your first ops list. Reading and writing in the same turn is supported — it is the standard read-modify-write pattern.

## Step 3 — Decide: hand off to finalize or loop

After Step 1 and Step 2 ops have been executed (you receive their results), inspect `improvement_plan.iteration_state` (call it `state` below).

**Transition to `finalize`** (`control.type="transition"`, `next_phase="finalize"`) when ANY of these holds — first match wins:

| Condition | termination_reason |
|---|---|
| `state.latest_eval.overall_score >= state.session.score_threshold` | `score_threshold_met` |
| `state.current_iteration >= state.session.max_iterations` | `max_iterations_reached` |
| `improvement_plan.changes` is empty | `no_more_changes_planned` |
| `state.current_iteration > 1` AND `state.latest_eval.overall_score < state.history[-1].eval_score` | `regression_detected` |
| `state.current_iteration > 1` AND `abs(state.latest_eval.overall_score - state.history[-1].eval_score) < 0.02` | `stagnation_detected` |

**Loop** (`control.type="rollback"`) ONLY when none of the above conditions holds. The rollback chains back through plan_improvements → run_and_eval, starting iteration N+1 with the just-modified DSL.

For rollback, set `control.reason.summary` to something like:
```
"iteration 2: score 0.65 < threshold 0.85; targeting weakest_phase=foo"
```

## Output (finalize path)

All path values MUST be read from `state.session._resolved_paths` — do NOT construct
path strings yourself. Every required field in `improvement_result` MUST be populated
from the session data already available in the input artifact (via `state.session`,
`state.history`, `state.latest_eval`, and `state.session._resolved_paths`). Do NOT
leave any required field as null or omit it — read the value from `_resolved_paths`
or compute it from the session fields directly.

Emit `improvement_result` with:

- `target_skill_path`: `state.session._resolved_paths.target_skill_path` (work-copy path)
- `iterations_performed`: `state.current_iteration` (i.e. length of `iterations` AFTER Step 2's append)
- `initial_score`: `state.history[0].eval_score` if `state.history` had ≥1 entry on entry to this phase; otherwise `state.latest_eval.overall_score`
- `final_score`: `state.latest_eval.overall_score`
- `score_history`: list of all eval_scores in iteration order, from `state.history` plus `state.latest_eval.overall_score` appended (length = `iterations_performed`)
- `files_modified`: deduplicated union of `files_modified` across all entries in the post-append iterations array
- `termination_reason`: from the table in Step 3
- `summary`: prose describing the score progression and what changed
- `next_steps`: a concrete command — typically `reyn eval <eval_spec_path>` where `eval_spec_path` = `state.session._resolved_paths.eval_spec_path`
- `work_skill_root`: `state.session._resolved_paths.target_skill_root` (the temp work directory)
- `original_skill_root`: `state.session._resolved_paths.original_skill_root` (the original skill directory)
- `copied_back`: `false` (finalize will update this field)

## Output (rollback path)

Emit `control.type="rollback"` with a clear `control.reason.summary`. The artifact field is ignored on rollback — emit `{"type": "rollback", "data": {}}` as a placeholder.

**CRITICAL — decide turn output format**: Every decide turn response MUST include a top-level `control` block with at least `type` and `decision` fields. The OS rejects responses that emit only `artifact` without `control`. Always structure your final output with both `control` and `artifact` at the top level.
