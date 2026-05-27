---
type: phase
name: explore
input: swe_bench_input
role: analyst
model_class: standard
allowed_ops: [file, grep, shell]
---

Understand the problem by reading the `problem_statement` and finding the
relevant code in the repository.  The goal is to identify the files most
likely to need editing.

## Step 1 — Read the problem statement

Read `problem_statement` from the input artifact.  If `hints_text` is
non-empty, use it as additional guidance for where to look.

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

Read the `test_patch` field from the input to understand what the tests expect
the fixed code to do.  This gives a precise specification: the fix must make
those tests pass.  Do NOT apply the test_patch now — that happens in verify.

## Step 5 — Record exploration findings

Summarize what you found: which files contain the bug, what the root cause
appears to be, and which code regions need to change.  This summary is passed
to the plan phase.

## When to transition

After the exploration is complete, transition to the plan phase with a
populated exploration artifact.
