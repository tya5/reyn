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

## Where to find the input fields

This phase receives an input artifact of type `swe_bench_input`. Its `data`
object contains every field you need. The shape (with ALL-CAPS placeholders
denoting values you must read from the actual artifact in the prompt — NOT
literal values to copy) is:

```json
{
  "type": "swe_bench_input",
  "data": {
    "instance_id": "<INSTANCE_ID_FROM_ARTIFACT>",
    "repo": "<REPO_SLUG_FROM_ARTIFACT>",
    "base_commit": "<COMMIT_SHA_FROM_ARTIFACT>",
    "problem_statement": "<GITHUB_ISSUE_BODY_FROM_ARTIFACT>",
    "hints_text": "<OPTIONAL_HINTS_FROM_ARTIFACT>",
    "test_patch": "<UNIFIED_DIFF_FROM_ARTIFACT>"
  }
}
```

**Critical**: the angle-bracketed `<…_FROM_ARTIFACT>` strings above are
documentation placeholders showing the SHAPE of the data. They are NOT
literal values you should copy into shell commands, file paths, grep
patterns, or any other tool call. The actual values live in the prompt's
input-artifact section; read them from there and inline the REAL values
(e.g. real owner/repo string like `astropy/astropy`, real 40-char SHA,
real issue body text) into your tool calls.

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
present in the input from Step 1 — you do NOT need to issue a shell or file
op to access it. The value lives at `data.test_patch` in the same input
artifact as `data.problem_statement`.

This gives a precise specification: the fix must make those tests pass. Do
NOT apply the test_patch now — that happens in verify.

## Step 5 — Record exploration findings

Summarize what you found: which files contain the bug, what the root cause
appears to be, and which code regions need to change.  This summary is passed
to the plan phase.

## When to transition

After the exploration is complete, transition to the plan phase with a
populated exploration artifact.
