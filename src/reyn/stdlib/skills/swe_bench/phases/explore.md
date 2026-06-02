---
type: phase
name: explore
input: swe_bench_input
role: analyst
model_class: standard
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files, sandboxed_exec]
max_act_turns: 20
# FP-0008 #1115 Stage 2: policy for any sandboxed_exec op this phase runs while
# exploring an arbitrary repository (e.g. git log / ls). Permissive; ignored by a
# container EnvironmentBackend (the C7 path), best-effort on host backends.
default_sandbox_policy:
  network: true
  read_paths: ["/"]
  write_paths: ["/"]
  allow_subprocess: true
  env_passthrough: ["PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV", "LANG", "LC_ALL", "TMPDIR"]
  timeout_seconds: 120
---

Understand the problem by reading the `problem_statement` and finding the
relevant code in the repository.  The goal is to identify the files most
likely to need editing.

## Where to find the input fields

This phase receives an input artifact of type `swe_bench_input`. Its `data`
object contains every field you need. The input shape is:

```json shape_only
{
  "type": "swe_bench_input",
  "data": {
    "instance_id": "django__django-12345",
    "repo": "django/django",
    "base_commit": "d16bfe05a744909de4b27f5875fe0d4ed41ce607",
    "problem_statement": "BUG: AttributeError when calling foo() on a queryset ...",
    "hints_text": "Look at django/db/models/query.py near the foo method",
    "test_patch": "diff --git a/tests/test_foo.py b/tests/test_foo.py\n..."
  }
}
```

All six fields are present in the prompt's artifact section that the OS
gives you (= no need to grep / search / probe to find them). Read them
directly from `data.*` — do NOT abort with "problem_statement missing"
before reading the artifact, because the fields ARE there. If you genuinely
cannot find them, recheck the prompt's input-artifact block before aborting.

## Step 1 — Read the problem statement

Read `data.problem_statement` from the input artifact. This is the GitHub
issue text describing the bug to fix. If `data.hints_text` is non-empty,
use it as additional guidance for where to look.

## Step 2 — Locate relevant code with grep

Use grep ops to search the repository for symbols, error messages, or
identifiers mentioned in the problem statement.  Typical searches:

- Exception class names or error strings quoted in the problem statement
- Function or method names that the issue describes as broken
- File paths mentioned in the issue or hints

Issue multiple grep ops as needed.  Prefer targeted patterns over broad
searches.

## Step 3 — Read the most relevant files

For each file identified in Step 2, issue file read ops to understand the
surrounding context.  Focus on the functions, classes, or code paths that the
problem statement references.  Read only what is needed — avoid loading the
entire repository.

## Step 4 — Inspect the test_patch to understand expected behavior

Read `data.test_patch` from the input artifact to understand what the tests
expect the fixed code to do.  This is a unified diff string that has been
present in the input from Step 1 — you do NOT need to issue any op to access
it. The value lives at `data.test_patch` in the same input artifact as
`data.problem_statement`.

This gives a precise specification: the fix must make those tests pass. Do
NOT apply the test_patch now — that happens in verify.

## Step 5 — Record exploration findings

Summarize what you found: which files contain the bug, what the root cause
appears to be, and which code regions need to change.  This summary is passed
to the plan phase.

## When to transition

After the exploration is complete, transition to the plan phase with a
populated exploration artifact.
