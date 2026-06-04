---
type: phase
name: plan
input: exploration | verify_state
role: architect
model_class: standard
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files]
max_act_turns: 15
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
