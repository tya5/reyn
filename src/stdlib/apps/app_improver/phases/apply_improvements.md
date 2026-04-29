---
type: phase
name: apply_improvements
input: improvement_plan
role: implementer
model_class: standard
can_finish: true
---

Apply the planned DSL changes, commit the iteration to history, and decide whether to loop or finish.

## Step 1 — Apply each change

For each entry in `improvement_plan.changes`:
- `create` or `update` → file write op with `path = <change.file>` and `content = <change.new_content>`
- `delete` → file delete op with `path = <change.file>`

File paths are project-relative; the runtime resolves them under the project root. No `dsl_patches/` indirection — write directly to the target paths.

If `changes` is empty, skip this step.

Collect the list of paths written/deleted into `applied_files` for use in Step 2.

## Step 2 — Commit the iteration to history

This step always runs, even when `changes` is empty.

1. Issue a file read for `improver_state.json`.
2. Append a new entry to its `iterations` array:
   ```json
   {
     "iteration":      <improvement_plan.iteration_state.current_iteration>,
     "eval_score":     <iteration_state.latest_eval.overall_score>,
     "weakest_phase":  <iteration_state.latest_eval.weakest_phase>,
     "files_modified": <applied_files from Step 1>,
     "plan_summary":   <improvement_plan.summary>
   }
   ```
3. Issue a file write op back to `improver_state.json` with the updated state.

Committing AFTER applying ensures that on rollback (Step 3 → loop), run_and_eval sees the updated history.

## Step 3 — Decide: finish or loop

Inspect `improvement_plan.iteration_state` (call it `state` below).

**Finish** (choose `control.type="finish"`) when ANY of the following conditions holds — list each one you check in `control.reason.summary`, and identify which condition triggered the finish in the output's `termination_reason` field:

| Condition | Termination reason |
|---|---|
| `state.latest_eval.overall_score >= state.session.score_threshold` | `score_threshold_met` |
| `state.current_iteration >= state.session.max_iterations` | `max_iterations_reached` |
| `improvement_plan.changes` is empty | `no_more_changes_planned` |
| `state.current_iteration > 1` AND `state.latest_eval.overall_score < state.history[-1].eval_score` | `regression_detected` |
| `state.current_iteration > 1` AND `abs(state.latest_eval.overall_score - state.history[-1].eval_score) < 0.02` | `stagnation_detected` |

Conditions are checked in the order above; the first match wins.

**Loop** (choose `control.type="rollback"`) ONLY when none of the finish conditions holds. The rollback chains back through plan_improvements → run_and_eval, beginning iteration N+1 with the just-modified DSL.

When emitting rollback, write a `control.reason.summary` that names the iteration index and the score gap, e.g.:
```
"iteration 2: score 0.65 < threshold 0.85; targeting weakest_phase=foo"
```

## Output (finish path)

Emit `improvement_result` with:

- `target_app_path`: from `state.session.target_app_path`
- `iterations_performed`: length of history AFTER Step 2's append (i.e. `state.current_iteration`)
- `initial_score`: `state.history[0].eval_score` if history had at least one entry before this iteration; otherwise `state.latest_eval.overall_score`
- `final_score`: `state.latest_eval.overall_score`
- `score_history`: `[h.eval_score for h in updated_iterations]` (the array AFTER Step 2's append, in iteration order)
- `files_modified`: deduplicated union of `files_modified` across all entries in updated_iterations
- `termination_reason`: from the table in Step 3
- `summary`: prose describing the score progression and what changed
- `next_steps`: a concrete command, e.g.:
  - `reyn eval <eval_spec_path>` — to verify the improvement
  - or `reyn run <target_app_path> '<case_input>'` — to inspect the live behavior

## Output (rollback path)

Emit ONLY `control.type="rollback"` with a clear `control.reason.summary`. The artifact field is ignored on rollback — emit `{"type": "rollback", "data": {}}` as a placeholder.
