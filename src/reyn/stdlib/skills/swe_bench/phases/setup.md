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

Issue a shell op to run:

```
git checkout <base_commit>
```

where `<base_commit>` is the value from the input artifact.  If the checkout
fails (non-zero exit code), abort with a clear message explaining the failure.

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
