---
type: phase
name: setup
input: swe_bench_input
role: initializer
model_class: standard
allowed_ops: [shell]
---

Prepare the repository for the fix by checking out the exact commit the
SWE-bench harness requires.

## Step 1 — Check out base_commit

The input artifact (`swe_bench_input`) carries a `base_commit` field whose
value is a git commit SHA (e.g. `"d16bfe05a744909de4b27f5875fe0d4ed41ce607"`).

Issue a shell op whose `cmd` is the literal command **with the SHA value
substituted in**. For example, when `base_commit` is
`"d16bfe05a744909de4b27f5875fe0d4ed41ce607"`, the shell op must look like:

```json
{"kind": "shell", "cmd": "git checkout d16bfe05a744909de4b27f5875fe0d4ed41ce607"}
```

**Critical**: do NOT emit a literal placeholder like `git checkout <base_commit>` —
that is template-style notation for documentation, not a runnable command. The
shell op's `cmd` field must contain the actual SHA, not the `<base_commit>`
placeholder. Read the SHA from the input artifact and inline it into the cmd
string.

If the checkout fails (non-zero exit code), abort with a clear message
explaining the failure.

## Step 2 — Confirm the working tree is clean

Issue a shell op to run `git status --short`.  If there are unexpected
uncommitted changes from a prior run (this can happen in repeated batch
evaluation), run `git checkout -- .` to reset them before proceeding.

## Step 3 — Verify test infrastructure is available

Issue a shell op to verify that a Python test runner is available.  A simple
`python -m pytest --version` (or `python -m unittest --version` as fallback)
is enough.  Record the outcome in `summary` — do NOT abort if the runner is
missing; note it and continue so the verify phase can detect the real failure.

## When to transition

After all three steps complete (with or without errors noted), transition to
the next phase.  The exploration phase will work regardless of whether the
test runner is confirmed.
