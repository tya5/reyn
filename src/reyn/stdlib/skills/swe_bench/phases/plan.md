---
type: phase
name: plan
input: exploration | verify_state
role: architect
model_class: standard
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files]
max_act_turns: 15
# #1366 — deterministic plan-time region scaffolding (plan-layer analogue of the
# apply #1209 block). BEFORE the LLM enters this phase, the OS places the
# problem-relevant regions of the candidate files into context: extract code
# symbols from the problem_statement (the legitimate task input — NOT test_patch,
# which would deepen leakage), then grep each symbol in the explore relevant_files.
# So the model copies a real `anchor` from a region it actually sees instead of
# fabricating one for a truncated-out-of-view large file (the apply-starvation
# root cause, surfaced one layer up). Deterministic (P5), never LLM-mutated.
# `extract_problem_symbols` returns [{file, symbol, symbol_re}] (cartesian of
# relevant_files x symbols); the iterate step greps each into `_plan_regions`.
preprocessor:
  - type: python
    module: ./extract_problem_symbols.py
    function: extract_problem_symbols
    mode: safe
    into: data._plan_symbols
    output_schema:
      type: array
  - type: iterate
    over: data._plan_symbols
    apply:
      type: run_op
      op:
        kind: file
        op: grep
        path: "__placeholder__"
        pattern: "__placeholder__"
        output_mode: content
        context_before: 40
        context_after: 40
      args_from:
        path: "_iter.item.file"
        pattern: "_iter.item.symbol_re"
      on_error: skip
    into: data._plan_regions
    on_error: skip
---

Produce a concrete edit plan: a list of files to change and a description of
what to change in each, sufficient for the apply phase to implement the fix.

## Domain rule — plan SOURCE files only

Plan edits to SOURCE files only.  Do not include test files in the edit plan.
The SWE-bench harness owns the test files via its own test_patch; apply-phase
test edits are reverted before verification and will not count.

## Context

The input is either:

- `exploration` (= attempt 1) — produced by the previous explore phase.
  Read its `data` fields (which files contain the bug, what the root cause
  appears to be, candidate edit targets) to ground the plan.
- `verify_state` (= attempt > 1) — a prior verify attempt failed. Read
  `data.failure_summary` to understand which tests failed and why, plus
  the exploration artifact saved to the workspace by the earlier explore
  phase.

In both cases the fields are present in the prompt's input-artifact block
(= the OS injects the artifact data structurally — no need to abort with
"exploration missing" before reading the prompt). You may ALSO need to
refer back to the original SWE-bench input fields (`problem_statement`,
`test_patch`) for context — those were carried into the workspace by the
explore phase and can be re-read via a file op against the exploration
summary if needed.

## Step 1 — Review the exploration summary

Read the workspace artifact from the explore phase. Look for the fields
explore.md recorded: which files contain the bug, what the root cause
appears to be, and which code regions need to change.

If the input artifact type is `verify_state`, also read its
`data.failure_summary` to understand which tests failed and why.

When re-planning (attempt > 1), first read the previous plan and the verify
failure summary from the workspace.  Avoid repeating edits that already
failed without changing the approach.

Set this plan's `attempt` to the input `verify_state`'s `attempt` + 1 (a plan
built from `exploration` is `attempt = 1`). Advancing the counter each re-plan
is what lets the verify-phase retry limit bound the loop — a plan that does not
increment would let the cycle run unbounded.

## Step 1.5 — Ground your anchors in the pre-fetched regions

The OS has already placed the problem-relevant code regions of the candidate
files into your input under `_plan_regions` — a `grep` of the symbols named in
the problem statement (with surrounding context) against the explore phase's
relevant files. Use these regions as the source of truth for the exact current
text when you choose each edit's `anchor`:

- Copy each `anchor` VERBATIM from a line you can see in a `_plan_regions` entry
  (or from a targeted read you issue) — never reconstruct it from memory. A
  fabricated anchor finds nothing at apply and the edit is dropped.
- If `_plan_regions` is empty or does not cover the region you need (the problem
  statement may not have named the target symbols), issue a targeted `read`
  (with `offset`/`limit`) or `grep` for that file to bring the region into view
  before writing the anchor. A bounded read stays in context (it is not
  offloaded out of view).

This is the same grounding discipline the apply phase uses; doing it here means
the anchors you emit are guaranteed to exist in the file.

## Step 2 — Re-read targeted code sections (if needed)

If the verify failure summary points to a code region not covered in the
original exploration, issue additional file read or grep ops to close the gap
before committing to a new plan.

## Step 3 — Formulate the edit plan

For each file that needs to change, describe:

- The specific function, method, or code block to modify
- What the change should be (add, remove, or replace logic)
- Why this change addresses the problem
- An **`anchor`**: a short, VERBATIM single-line snippet copied EXACTLY from the
  current file at the edit site. This is a grep landmark — the apply phase greps
  it to place the edit's target region into context automatically, so apply
  never edits a file it cannot see. Requirements:
  - Copy it character-for-character from a line you actually read (do not
    paraphrase or reconstruct from memory) — a mismatched anchor finds nothing.
  - Choose a line **unique** within the file (a distinctive signature,
    assignment, or comment) so the grep returns exactly one match.
  - For an **addition** (new function/import/block), anchor on the nearest
    existing line where the insertion goes.

The plan should be precise enough that the apply phase can execute it without
further analysis.  Prefer minimal, targeted changes over large rewrites.

## When to transition

Once the plan is ready, transition to the apply phase.
