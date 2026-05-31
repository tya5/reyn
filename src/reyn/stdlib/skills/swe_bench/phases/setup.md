---
type: phase
name: setup
input: swe_bench_input
role: initializer
model_class: standard
allowed_ops: [sandboxed_exec]
max_act_turns: 5
# FP-0008 #1115 Stage 2: policy for this phase's sandboxed_exec ops (git
# checkout / git status / pytest --version), winning over op fields. Permissive
# — operates on an arbitrary repository. Ignored by a container EnvironmentBackend
# (the C7 path); best-effort on host backends.
default_sandbox_policy:
  network: true
  read_paths: ["/"]
  write_paths: ["/"]
  allow_subprocess: true
  env_passthrough: ["PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV", "LANG", "LC_ALL", "TMPDIR"]
  timeout_seconds: 120
---

Prepare the repository for the fix by checking out the exact commit the
SWE-bench harness requires.

## Step 1 — Check out base_commit

The input artifact (`swe_bench_input`) carries a `base_commit` field whose
value is a git commit SHA (e.g. `"d16bfe05a744909de4b27f5875fe0d4ed41ce607"`).

Run `git checkout` against that SHA, **with the actual value from the artifact
substituted in**. For example, when `base_commit` is
`"d16bfe05a744909de4b27f5875fe0d4ed41ce607"`, run:

```
git checkout d16bfe05a744909de4b27f5875fe0d4ed41ce607
```

**Critical**: do NOT run a literal placeholder like `git checkout <base_commit>` —
that is template-style notation for documentation, not a runnable command. Read
the actual SHA from the input artifact and use it; never the `<base_commit>`
placeholder text.

If the checkout fails (non-zero exit code), abort with a clear message
explaining the failure.

## Step 2 — Confirm the working tree is clean

Run `git status --short`.  If there are unexpected uncommitted changes from a
prior run (this can happen in repeated batch evaluation), run
`git checkout -- .` to reset them before proceeding.

## Step 3 — Verify test infrastructure is available

Run a quick check that a Python test runner is available.  A simple
`python -m pytest --version` (or `python -m unittest --version` as fallback)
is enough.  Record the outcome in `summary` — do NOT abort if the runner is
missing; note it and continue so the verify phase can detect the real failure.

## When to transition

After all three steps complete (with or without errors noted), transition to
the next phase.  The exploration phase will work regardless of whether the
test runner is confirmed.
