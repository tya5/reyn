---
type: phase
name: report
input: verify_state
role: reporter
model_class: standard
can_finish: true
allowed_ops: [shell]
max_act_turns: 10
preprocessor:
  # FP-0008 C6 Part 2: read the original swe_bench_input artifact from
  # the workspace to access test_patch (same source as verify preprocessor).
  # on_error: empty — if absent (unit tests), the revert step no-ops cleanly.
  - type: run_op
    op:
      kind: file
      op: read
      path: ".reyn/artifacts/swe_bench/_input/v01_swe_bench_input.json"
    into: data._input_raw
    on_error: empty
  # Capture repo working directory (shell ops run with cwd=workspace.base_dir).
  - type: run_op
    op:
      kind: shell
      cmd: pwd
      timeout: 5
    into: data._repo_dir
    on_error: empty
  # Revert test_patch-target files to HEAD so `git diff HEAD` (Step 1)
  # produces a source-only diff. After verify's Step 3 (test_patch reversal)
  # these files should already be clean; this step is a belt-and-suspenders
  # guard that ensures the final solution patch is never contaminated by
  # apply-phase test edits, regardless of verify phase state.
  - type: python
    module: ./revert_test_targets.py
    function: revert_test_targets
    mode: safe
    into: data._revert_result
    output_schema:
      type: object
      properties:
        reverted:
          type: array
          items: {type: string}
        errors:
          type: array
          items: {type: object}
      required: [reverted, errors]
---

Produce the final output by capturing the git diff of all changes made during
this skill run.

## Step 1 — Capture the git diff

Issue a shell op:

```
git diff HEAD
```

The output is the unified diff of all edits applied by the apply phase(s)
relative to the base_commit.  This is the patch the SWE-bench harness will
apply to the pristine base_commit repository when evaluating the submission.

If `git diff HEAD` produces no output (no changes), record an empty string
for the patch — this is a valid (though unsuccessful) submission.

## Step 2 — Record the final result

Collect the following from the input and the diff output:

- `instance_id`: from `verify_state.instance_id`
- `patch`: the raw string output of `git diff HEAD`
- `tests_passed`: from `verify_state.tests_passed`
- `attempts`: from `verify_state.attempt`

**Validation contract**: `swe_bench_result` enforces that `tests_passed=true`
ONLY when `patch` is non-empty. If `git diff HEAD` produced an empty patch
(= no code changes were made by the apply phase), set `tests_passed=false`
— an empty-patch "pass" is a no-op submission and the schema rejects it.

## When to finish

After the diff is captured, finish the skill by emitting `swe_bench_result`
with all four fields populated.  This is the terminal phase — no further
transitions are possible.
