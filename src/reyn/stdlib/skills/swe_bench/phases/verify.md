---
type: phase
name: verify
input: apply_state
role: tester
model_class: standard
allowed_ops: [file, shell]
max_retries: 3
---

Run the SWE-bench test patch against the current codebase to determine whether
the fix is correct.

## Step 1 — Apply the test_patch

The `test_patch` field in the input contains a unified diff that adds or
modifies test files.  Apply it with:

```
git apply --check <test_patch_content>
```

Write `test_patch` to a temporary file (e.g. `.reyn/swe_bench_test.patch`)
then run:

```
git apply .reyn/swe_bench_test.patch
```

If `git apply` fails, record the failure in `failure_summary` and treat this
as a verify execution failure (not a test failure) — transition back to apply
so the patch can be re-examined.

## Step 2 — Run the tests

After the patch is applied, run the test files that were added or modified by
the patch.  Determine the test file paths from the diff header lines
(`+++ b/<path>`).

Issue a shell op:

```
python -m pytest <test_file_path> -x --tb=short 2>&1
```

If pytest is not available, fall back to:

```
python -m unittest <test_module> 2>&1
```

## Step 3 — Revert the test_patch

After the test run (pass or fail), revert the test patch to restore the
repository to the state expected by the harness:

```
git checkout -- <test_file_paths>
```

or:

```
git apply --reverse .reyn/swe_bench_test.patch
```

The SWE-bench harness applies the test patch itself — the skill must NOT leave
the test files applied when it produces the final diff.

## Step 4 — Evaluate the outcome

Inspect the test runner's exit code and output:

- Exit code 0, all tests collected and passed → `tests_passed = true` → transition to report
- Non-zero exit code, test failures reported → `tests_passed = false` → transition back to plan
  (the plan phase will revise the fix based on `failure_summary`)

Record a concise `failure_summary` when tests fail: which test names failed,
the assertion error messages, and any relevant tracebacks.  This summary is
the primary input for the next plan phase.

## Retry limit

If `attempt` has reached the maximum allowed (3 by default), set
`tests_passed = false` and transition to report anyway — the skill reports the
best-effort patch even when tests did not pass, matching SWE-bench harness
expectations.
