---
type: phase
name: verify
input: apply_state
role: tester
model_class: standard
allowed_ops: [file, sandboxed_exec]
max_retries: 3
max_act_turns: 30
# FP-0008 #1115 Stage 2 (D mechanism): the OS applies this policy to every
# sandboxed_exec op in this phase, winning over the op's own fields (the LLM
# cannot weaken or strengthen it). This phase runs an arbitrary repository's
# git + test suite, which inherently needs broad filesystem + subprocess +
# network access — so the policy is permissive. On a container EnvironmentBackend
# (the C7 path) the policy is ignored entirely (the container IS the boundary);
# on host backends it is best-effort enforcement. timeout_seconds bounds the
# longest op (pytest).
default_sandbox_policy:
  network: true
  read_paths: ["/"]
  write_paths: ["/"]
  allow_subprocess: true
  env_passthrough: ["PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV", "LANG", "LC_ALL", "TMPDIR"]
  timeout_seconds: 600
preprocessor:
  # FP-0008 PR-N15 / #1115 Stage 0: deterministic entry-input passthrough.
  # The OS injects the skill's original entry artifact (the `swe_bench_input`)
  # at the reserved `_skill_input` binding before this preprocessor runs. This
  # is the P5-correct source of truth for test_patch — it is the OS-held entry
  # input and never passes through the apply LLM, so it can never be dropped or
  # nulled by a weak model. No workspace file.read is needed (the prior
  # `.reyn/artifacts/...` magic-path read was removed in #1115 Stage 0 — that
  # path coupled the read to base_dir, which breaks once the repo FS routes
  # through a backend). sanitize_test_patch / parse_test_targets read
  # `_skill_input.data.test_patch` directly, falling back to `data.test_patch`
  # for unit tests that inject the verify input directly.
  #
  # FP-0008 PR-O v8: sanitize_test_patch normalizes line endings (CRLF → LF),
  # strips BOM, ensures a trailing newline. Runs BEFORE the LLM enters this
  # phase. The sanitized string is written into `data.test_patch` so
  # `git apply` operates on a clean diff.
  - type: python
    module: ./sanitize_test_patch.py
    function: sanitize_test_patch
    mode: safe
    into: data.test_patch
    output_schema:
      type: string
  #
  # FP-0008 C6 v2 Step 3: parse test_patch targets → list of git-checkout
  # argv lists.  Pure string transform (re + json only, mode: safe).
  # Output: [["git","checkout","HEAD","--","tests/test_x.py"], ...] — zero or more.
  # The function itself returns [] on absent/empty test_patch (graceful no-op).
  - type: python
    module: ./parse_test_targets.py
    function: parse_test_targets
    mode: safe
    into: data._revert_cmds
    output_schema:
      type: array
      items:
        type: array
        items: {type: string}
  #
  # FP-0008 C6 v2 Step 4 (#1115 Stage 2): iterate over argv lists and run each
  # via sandboxed_exec run_op.  sandboxed_exec anchors the subprocess to
  # cwd=workspace.base_dir (FP-0008 PR-I, restored for sandboxed_exec in the
  # cwd-anchor PR) = the SWE-bench repo root, so git checkout operates on the
  # correct working tree even in concurrent benchmark runs, and routes through
  # the run's EnvironmentBackend (host or container) instead of the deprecated
  # shell op.  The phase default_sandbox_policy (above) governs the policy.
  #
  # args_from {argv: "_iter.item"} is resolved by _materialize_op:
  # the IterateStep injects {_iter: {item: <argv_list>}} into iter_artifact
  # before calling _materialize_op, which resolves the dot-path "_iter.item"
  # to the current argv list and replaces SandboxedExecIROp.argv via
  # model_copy(update={"argv": argv_list}).
  #
  # on_error: skip — a path not in HEAD (= new test file) returns non-zero
  # from git checkout; skip it rather than aborting the whole preprocessor.
  - type: iterate
    over: data._revert_cmds
    apply:
      type: run_op
      op:
        kind: sandboxed_exec
        argv: ["git", "checkout", "HEAD", "--", "__placeholder__"]
      args_from:
        argv: "_iter.item"
      on_error: skip
    into: data._revert_results
    on_error: skip
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
flags, treat this as a verify execution failure (not a test failure):
set `tests_passed = false` and record the failure in `failure_summary`
(= include the exact stderr from git apply, NOT a vague "patch failed"
summary). The tests could not be evaluated, so this is not a pass.

**Do NOT skip the test_patch** — an unapplied test patch means tests
can't be evaluated; that's NOT a pass. The schema invariant
(`oneOf` on `swe_bench_result`) rejects `tests_passed=true` with an
empty patch, but an unapplied test_patch could still produce a
mis-attributed `tests_passed=true` if the LLM bypasses Step 1.

## Step 2 — Run the tests

After the patch is applied, run the test files that were added or modified by
the patch.  Determine the test file paths from the diff header lines
(`+++ b/<path>`).

Run:

```
python -m pytest <test_file_path> -x --tb=short
```

If pytest is not available, fall back to:

```
python -m unittest <test_module>
```

Both stdout and stderr are captured for you — do not append shell redirections
(`2>&1`); the command runs directly (not through a shell), so a redirection
token would be passed as a literal argument.

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

Inspect the test runner's exit code and output and record the verdict:

- Exit code 0, all tests collected and passed → `tests_passed = true`,
  `failure_summary = ""`.
- Non-zero exit code, test failures reported → `tests_passed = false`, and
  record a concise `failure_summary`: which test names failed, the assertion
  error messages, and any relevant tracebacks.

Whether the tests passed or failed, the verdict is captured in `tests_passed`
+ `failure_summary` — the downstream phase consumes that outcome. The skill
always carries the best-effort patch forward, even when the tests did not pass
(matching SWE-bench harness expectations).

## Retry limit

If `attempt` has reached the maximum allowed (3 by default), set
`tests_passed = false` and record the best-effort outcome — the skill reports
the patch even when the tests did not pass, matching SWE-bench harness
expectations.

## Convergence guard — MANDATORY

If `git apply` (or `git apply --check`) has failed **3 or more consecutive
times** with the same error (same `returncode`, same `stderr` substring), STOP
attempting to apply the patch.  In either of these cases — "No valid patches
in input" (the patch you wrote is invalid) or a context-line conflict (the
repository state differs from what the patch expects) — set
`tests_passed = false` and record the exact `git apply` error in
`failure_summary`.

Do NOT write a new version of the same patch and retry `git apply` in a loop.
Each identical write+apply pair consumes 2 turns with zero forward progress.
After 3 consecutive failures, treating the error as structural and recording
the best-effort outcome is always more productive than additional retries.
