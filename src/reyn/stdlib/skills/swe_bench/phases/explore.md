---
type: phase
name: explore
input: swe_bench_input
role: analyst
model_class: standard
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files, sandboxed_exec]
max_act_turns: 20
# #1375 D2 — deterministic file-candidate scaffolding (explore-layer analogue of
# the plan region-surfacing #1366). BEFORE the LLM enters this phase, the OS
# pre-greps the problem_statement's code-symbols across the repo and surfaces the
# strongest candidate files (ranked by symbol co-occurrence + specificity) into
# `_candidate_files`, so the weak explore model SEES the gold files in context
# instead of missing them (astropy-13398: gold in builtin_frames/* that explore
# overlooked). Deterministic (P5), never LLM-mutated. `extract_explore_symbols`
# yields the symbols; the iterate greps each across the repo (files_with_matches);
# `rank_candidate_files` ranks the matched files.
preprocessor:
  - type: python
    module: ./extract_problem_symbols.py
    function: extract_explore_symbols
    mode: safe
    into: data._explore_symbols
    output_schema:
      type: array
  - type: iterate
    over: data._explore_symbols
    apply:
      type: run_op
      op:
        kind: file
        op: grep
        path: "."
        pattern: "__placeholder__"
        output_mode: files_with_matches
      args_from:
        pattern: "_iter.item.symbol_re"
      on_error: skip
    into: data._symbol_files
    on_error: skip
  - type: python
    module: ./extract_problem_symbols.py
    function: rank_candidate_files
    mode: safe
    into: data
    output_schema:
      type: object
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

## Step 1.5 — Start from the pre-fetched candidate files

The OS has already grepped the problem statement's code-symbols across the repo
and placed the strongest candidate files into `data._candidate_files` (ranked by
how many problem-symbols they contain, with rare/specific symbols weighted
higher). These are the files most likely to contain the bug — **read these
first** and treat them as the leading candidates for `relevant_files`. They are
deterministically derived, so a file here is a real match, not a guess.

`_candidate_files` is a starting point, not a limit: if the problem statement
points elsewhere, still follow it (Step 2). When `_candidate_files` is empty
(the problem statement named no greppable symbols), fall back to Step 2 grep.

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
