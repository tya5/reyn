---
type: phase
name: finalize
input: improvement_result
role: finalizer
can_finish: true
allowed_ops: [file]
---

Copy improved files back to the original skill directory if the score threshold was met, then emit the final result.

All path values come from `improvement_result` fields which were populated by apply_improvements
from `session._resolved_paths`. Do NOT construct path strings yourself.

## Step 1 — Determine whether to copy back

Copy back **only** when ALL of the following hold:

1. `improvement_result.termination_reason == "score_threshold_met"`
2. `improvement_result.original_skill_root` is non-empty
3. `improvement_result.original_skill_root` does NOT start with `src/` (stdlib paths are outside the write zone — writes will be denied)

If all three conditions hold, proceed to Step 2.
Otherwise, skip to Step 3 (no copy).

## Step 2 — Copy improved files back to original

For each path in `improvement_result.files_modified`:

1. Compute the original path by replacing the `work_skill_root` prefix with `original_skill_root`:
   - Strip `<work_skill_root>/` from the start of the path to get the relative path
   - Prepend `<original_skill_root>/`
   - Example: `.reyn/skill_improver_work/my_app/phases/review.md` → `reyn/local/my_app/phases/review.md`

2. Issue a `file read` op for the work-dir path.
   - If the file **exists** (was created or updated): issue a `file write` op to the original path with the read content.
   - If the file does **not exist** (was deleted by `apply_improvements`): issue a `file delete` op for the original path.

Batch all read ops in one act turn and all write/delete ops in the next act turn.

After the writes complete, set `copied_back = true`.

## Step 3 — Emit final result

Emit `improvement_result` with all input fields unchanged except:

- `copied_back`: `true` if Step 2 completed, `false` if skipped.
- `summary`: if `copied_back == true`, append a sentence noting that the improved files were written back to `original_skill_root`.
- `next_steps`:
  - If `copied_back == true`: `"reyn eval <eval_spec_path>"` (verify the copy-back).
  - If `copied_back == false` and `termination_reason != "score_threshold_met"`: include a note that the work directory at `work_skill_root` is available for inspection.
  - If `copied_back == false` and `original_skill_root` starts with `src/`: note that the improved files are in `work_skill_root` and must be copied manually (stdlib is outside the write zone).
