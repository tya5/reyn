---
type: phase
name: finalize
input: improvement_result
role: finalizer
can_finish: true
allowed_ops: [file]
preprocessor:
  # FP-0006 Component B — snapshot the pre-apply skill.md to
  # .reyn/skill-versions/<name>/v<N>.md BEFORE copy-back happens.
  # Runs in unsafe mode: reads original skill.md from disk + writes snapshot
  # files + manages the `current` pointer file in the versions directory.
  # Only executed when termination_reason == "score_threshold_met" AND
  # original_skill_root is non-empty and not under src/ (stdlib guard).
  # Safe to run unconditionally — save_snapshot checks these conditions itself
  # and returns a noop result when copy-back would be skipped.
  - type: python
    module: ./version_snapshot.py
    function: save_snapshot
    into: data._snapshot
    output_schema:
      type: object
      properties:
        saved_version:       {}
        snapshot_path:       {type: string}
        next_version:        {}
        versions_dir:        {type: string}
        original_skill_root: {type: string}
      required: [saved_version, snapshot_path, next_version, versions_dir, original_skill_root]

  # FP-0006 Component D (R-PURE-MODE Wave 3b) — read on_propose config so the
  # LLM can decide whether to apply, ask the user, or dry-run.
  # Split into two steps:
  #   Step D-1: file_read op reads reyn.yaml text (OS-level, gated by file
  #             permission). on_error: skip so a missing reyn.yaml falls back
  #             to defaults in step D-2.
  #   Step D-2: pure regex parser extracts the self_improvement subset
  #             (mode: safe — no fs I/O, only re + builtins).
  - type: run_op
    op:
      kind: file
      op: read
      path: reyn.yaml
    into: data._reyn_yaml_text
    on_error: skip

  - type: python
    module: ./version_snapshot_pure.py
    function: parse_on_propose_config_minimal
    into: data._on_propose
    mode: safe
    timeout: 5
    output_schema:
      type: object
      properties:
        on_propose:   {type: string}
        max_versions: {type: integer}
      required: [on_propose, max_versions]
---

Copy improved files back to the original skill directory if the score threshold was met, then emit the final result.

All path values come from `improvement_result` fields which were populated by apply_improvements
from `session._resolved_paths`. Do NOT construct path strings yourself.

The preprocessor has already run two steps:
- `data._snapshot` — version snapshot info (Component B): `saved_version`, `snapshot_path`, `next_version`, `versions_dir`
- `data._on_propose` — config gate (Component D): `on_propose` ∈ {ask_user, auto, disabled}

## Step 0 — Check the on_propose gate (FP-0006 Component D)

Read `data._on_propose.on_propose`. This determines what happens when the score threshold was met:

- **`disabled`**: skip copy-back entirely. Emit `control.type="finish"` with `copied_back=false` and `termination_reason` unchanged. Include in `summary`: "Dry-run mode (on_propose: disabled) — improved files are in `work_skill_root` but were NOT applied." Emit a `skill_improvement_dry_run` note in `next_steps`.
- **`ask_user`** (default): before applying, issue an `ask_user` op with:
  - prompt: "Apply improved skill files to `<original_skill_root>`?"
  - detail: "Score: `<initial_score>` → `<final_score>` | Files: `<files_modified count>` | Snapshot saved as `<data._snapshot.snapshot_path>`"
  - suggestions: ["yes", "no"]
  Wait for the answer. If the answer is "no" (or any non-"yes" response): skip copy-back, set `copied_back=false`, note refusal in `summary`. If "yes": proceed to Step 1.
- **`auto`**: proceed to Step 1 directly (CI mode — no prompt).

If `termination_reason != "score_threshold_met"`, skip the gate entirely and go straight to Step 3 (no copy regardless of on_propose).

## Step 1 — Determine whether to copy back

Copy back **only** when ALL of the following hold:

1. `improvement_result.termination_reason == "score_threshold_met"`
2. `improvement_result.original_skill_root` is non-empty
3. `improvement_result.original_skill_root` does NOT start with `src/` (stdlib paths are outside the write zone — writes will be denied)
4. The on_propose gate (Step 0) approved the apply (not `disabled`, not user-refused)

If all conditions hold, proceed to Step 2.
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

3. After the writes complete, issue a `file write` op to update the version pointer:
   - path: `<data._snapshot.versions_dir>/current`
   - content: `<data._snapshot.next_version>` (as a plain integer string, e.g. "2")
   This records that the just-applied version is now live so `reyn skill versions` shows the correct current.

Batch all read ops in one act turn and all write/delete ops (including the pointer update) in the next act turn.

After the writes complete, set `copied_back = true`.

## Step 3 — Emit final result

Emit `improvement_result` with all input fields unchanged except:

- `copied_back`: `true` if Step 2 completed, `false` if skipped.
- `summary`: if `copied_back == true`, append a sentence noting that the improved files were written back to `original_skill_root` and the pre-apply snapshot is at `data._snapshot.snapshot_path`.
- `next_steps`:
  - If `copied_back == true`: `"reyn eval <eval_spec_path>"` (verify the copy-back). Optionally mention `reyn skill versions <skill_name>` to see version history.
  - If `copied_back == false` and `termination_reason != "score_threshold_met"`: include a note that the work directory at `work_skill_root` is available for inspection.
  - If `copied_back == false` and `original_skill_root` starts with `src/`: note that the improved files are in `work_skill_root` and must be copied manually (stdlib is outside the write zone).
  - If `copied_back == false` and `on_propose == "disabled"`: note that this was a dry-run; use `on_propose: auto` in reyn.yaml to apply automatically.
