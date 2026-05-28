---
type: phase
name: verify
input: apply_state
role: tester
model_class: standard
allowed_ops: [file, shell]
max_retries: 3
max_act_turns: 30
preprocessor:
  # FP-0008 PR-O v8: deterministic test_patch sanitizer normalizes
  # line endings (CRLF → LF), strips BOM, ensures trailing newline.
  # Runs BEFORE the LLM enters this phase. The sanitized string is
  # written into `data.test_patch`, replacing the LLM-visible field
  # so `git apply` operates on a clean diff.
  - type: python
    module: ./sanitize_test_patch.py
    function: sanitize_test_patch
    mode: safe
    into: data.test_patch
    output_schema:
      type: string
---

Run the SWE-bench test patch against the current codebase to determine whether
the fix is correct.

## Step 1 — Apply the test_patch

The `data.test_patch` field in the input is a unified diff. The
verify-phase preprocessor has already normalized it deterministically
(= CRLF → LF, BOM stripped, trailing newline ensured) so `git apply`
operates on a clean diff.

Write the sanitized diff to a temp file (e.g. `.reyn/swe_bench_test.patch`)
then apply with robust flags:

```
git apply --3way --recount --whitespace=fix .reyn/swe_bench_test.patch
```

The flags layer second-line defenses on top of the preprocessor:
`--3way` enables merge-style recovery on context drift, `--recount`
tolerates off-by-N context line counts, and `--whitespace=fix`
forgives trailing-space drift.

If `git apply` STILL fails after preprocessor sanitization + robust
flags, record the failure in `failure_summary` (= include the exact
stderr from git apply, NOT a vague "patch failed" summary) and treat
this as a verify execution failure (not a test failure) — transition
back to apply so the patch can be re-examined.

**Do NOT skip the test_patch** — an unapplied test patch means tests
can't be evaluated; that's NOT a pass. The schema invariant
(`oneOf` on `swe_bench_result`) rejects `tests_passed=true` with an
empty patch, but an unapplied test_patch could still produce a
mis-attributed `tests_passed=true` if the LLM bypasses Step 1.

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

## Convergence guard — MANDATORY

If `git apply` (or `git apply --check`) has failed **3 or more consecutive
times** with the same error (same `returncode`, same `stderr` substring), STOP
attempting to apply the patch.  Do ONE of:

- If the error is "No valid patches in input" or a similar content-empty
  error: the patch you wrote is invalid.  Transition back to **apply** so the
  fix can be re-examined.
- If the error is a context-line conflict: the repository state differs from
  what the patch expects.  Transition back to **apply** with the failure
  summary.

Do NOT write a new version of the same patch and retry `git apply` in a loop.
Each identical write+apply pair consumes 2 turns with zero forward progress.
After 3 consecutive failures, treating the error as structural and transitioning
is always more productive than additional retries.
