---
type: phase
name: plan
input: exploration | verify_state
role: architect
model_class: standard
allowed_ops: [file, grep]
---

Produce a concrete edit plan: a list of files to change and a description of
what to change in each, sufficient for the apply phase to implement the fix.

## Context

The input is either:

- `exploration` — first plan for this instance (attempt 1)
- `verify_state` — a prior verify attempt failed; use `failure_summary` plus
  the exploration notes from the workspace to revise the plan

## Step 1 — Review the exploration summary

Read the workspace artifact from the explore phase.  If the input is a
`verify_state`, also read `failure_summary` to understand which tests failed
and why.

When re-planning (attempt > 1), first read the previous plan and the verify
failure summary from the workspace.  Avoid repeating edits that already failed
without changing the approach.

## Step 2 — Re-read targeted code sections (if needed)

If the verify failure summary points to a code region not covered in the
original exploration, issue additional file read or grep ops to close the gap
before committing to a new plan.

## Step 3 — Formulate the edit plan

For each file that needs to change, describe:

- The specific function, method, or code block to modify
- What the change should be (add, remove, or replace logic)
- Why this change addresses the problem

The plan should be precise enough that the apply phase can execute it without
further analysis.  Prefer minimal, targeted changes over large rewrites.

## When to transition

Once the plan is ready, transition to the apply phase.
