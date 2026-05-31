---
type: phase
name: apply
input: plan
role: implementer
model_class: standard
allowed_ops: [file, sandboxed_exec]
max_act_turns: 30
# FP-0008 #1115 Stage 2: policy for this phase's sandboxed_exec ops (e.g.
# python -m py_compile syntax checks), winning over op fields. Permissive —
# operates on an arbitrary repository. Ignored by a container EnvironmentBackend
# (the C7 path); best-effort on host backends.
default_sandbox_policy:
  network: true
  read_paths: ["/"]
  write_paths: ["/"]
  allow_subprocess: true
  env_passthrough: ["PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV", "LANG", "LC_ALL", "TMPDIR"]
  timeout_seconds: 120
---

Implement the edit plan by modifying the repository files.

## Domain rule — edit SOURCE files only

Edit SOURCE files only.  The SWE-bench harness owns the test files: it applies
the test_patch itself after the skill run.  The verify preprocessor reverts any
test-file edits before running `git apply test_patch`, so apply-phase test edits
do not count and will not survive into the final diff.

## Step 1 — Read each file before editing

For every file listed in the plan's edits, issue a file read op to retrieve
the current content.  Do NOT edit from memory — always read first to ensure
the edit target is accurate.

## Step 2 — Apply each edit

For each entry in the plan, issue the appropriate file op:

- Use an edit op for targeted replacements within an existing file
- Use a write op only when creating a new file or replacing the entire content
  of a file

Apply edits in the order they appear in the plan.  After each edit, confirm
that the op succeeded before proceeding to the next.

## Step 3 — Basic syntax check (when applicable)

If the edited files are Python, run `python -m py_compile <file>` on each
modified file.  If a compile error is reported, correct the syntax before
transitioning.

For non-Python files, skip this step.

## Step 4 — Record what was changed

Collect the repository-relative paths of all files that were modified.

## When to transition

After all edits are applied (and syntax is clean if applicable), transition to
the verify phase.

## Convergence guard — MANDATORY

If you have already read the same file **3 or more times in a row** without
issuing an edit or write op on it, STOP reading and do ONE of:

- Issue the edit or write op you have been preparing (= commit to a change),
  OR
- Transition to verify with the edits completed so far.

Do NOT issue another read on the same file if the previous 2 turns were also
reads of that file.  Re-reading accumulates results in the context without
advancing the plan.  The accumulated read results will NOT change — the file
content is fixed until an edit op is issued.

Similarly, if you have attempted the same command (same argv) **3 or more
consecutive times** and it keeps failing with the same error, STOP and
transition rather than retrying.  Repeated command failures with identical
error output indicate a structural problem that additional retries will not
resolve within this budget.
