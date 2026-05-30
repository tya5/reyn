---
type: phase
name: report
input: verify_state
role: reporter
model_class: standard
can_finish: true
allowed_ops: [shell]
max_act_turns: 10
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
