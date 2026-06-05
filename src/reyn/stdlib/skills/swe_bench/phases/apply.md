---
type: phase
name: apply
input: plan
role: implementer
model_class: standard
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files, sandboxed_exec]
max_act_turns: 30
# #1209 PR-B — deterministic edit-region scaffolding. BEFORE the LLM enters this
# phase, the OS places each edit's target region into context by grepping the
# plan's verbatim `anchor`, so the model never edits a file it cannot see (the
# apply-starvation root cause: a large file was offloaded out of view and the
# model fabricated non-existent old_strings). Deterministic (P5), never
# LLM-mutated. `escape_anchors` regex-escapes each anchor (grep compiles regex);
# the iterate step greps each anchor (±50 lines of context) into `_edit_regions`.
preprocessor:
  - type: python
    module: ./escape_anchors.py
    function: escape_anchors
    mode: safe
    runs_in: os   # #183: OS-orchestration text-prep (pure transform) — host, never the agent container
    into: data.edits
    output_schema:
      type: array
  - type: iterate
    over: data.edits
    apply:
      type: run_op
      op:
        kind: file
        op: grep
        path: "__placeholder__"
        pattern: "__placeholder__"
        output_mode: content
        context_before: 50
        context_after: 50
      args_from:
        path: "_iter.item.file"
        pattern: "_iter.item.anchor_re"
      on_error: skip
    into: data._edit_regions
    on_error: skip
  # #1216: deterministically DROP not-locatable edits (a region with count 0 =
  # anchor not found) from the actionable plan + record them in `not_locatable`,
  # so the apply model has no anchored region to blind-edit from (a structural
  # close, not reliant on the model honouring a "skip count-0" instruction —
  # the #1216 run2 failure was the model ignoring exactly that instruction).
  - type: python
    module: ./drop_not_locatable.py
    function: drop_not_locatable
    mode: safe
    runs_in: os   # #183: OS-orchestration text-prep (pure transform) — host, never the agent container
    into: data
    output_schema:
      type: object
---

Implement the edit plan by modifying the repository files.

## Domain rule — edit SOURCE files only

Edit SOURCE files only.  The SWE-bench harness owns the test files: it applies
the test_patch itself after the skill run.  The verify preprocessor reverts any
test-file edits before running `git apply test_patch`, so apply-phase test edits
do not count and will not survive into the final diff.

## Step 1 — Ground each edit in its pre-fetched region

The OS has already placed each edit's target region into your input under
`_edit_regions` (one entry per edit, in plan order) — a `grep` of the plan's
`anchor` with surrounding context. Use these regions as the source of truth for
the exact current text; do NOT edit from memory.

For each edit, check its `_edit_regions` entry:

- **One match** → use the matched line and its surrounding context as the basis
  for the edit's `old_string` (copy the exact current text).
- **No match** (`count` is 0) → you will NOT see such an edit: the OS
  preprocessor has already **removed** every not-locatable edit (region count 0)
  from your plan and recorded it under `not_locatable`. So every edit you receive
  has a real region — never fabricate an `old_string` for an anchor you cannot
  see in a region (the removed ones are handled deterministically; the verify
  phase surfaces the `not_locatable` gap for a re-plan).
- **Multiple matches** (`count` > 1) → the anchor was not unique. Use the match
  whose surrounding context best fits the plan's `description` for that edit.

If a region is large or you need more than the ±context shown, issue a targeted
`read` (with `offset`/`limit`) for that file — a bounded read stays in context
(it is not offloaded out of view).

## Step 2 — Apply each edit

For each entry in the plan, issue the appropriate file op:

- Use an edit op for targeted replacements within an existing file
- Use a write op only when creating a new file or replacing the entire content
  of a file

Apply edits in the order they appear in the plan.  After each edit, confirm
that the op succeeded before proceeding to the next.

## Step 3 — Basic syntax check (when applicable)

If the edited files are Python, run `python -m py_compile <file>` on each
modified file.  If a compile error is reported, correct the syntax before
transitioning.

For non-Python files, skip this step.

## Step 4 — Record what was changed

Collect the repository-relative paths of all files that were modified.

If your input carries a `not_locatable` list (edits the preprocessor dropped
because their anchor matched no region in the target file), preserve each
dropped edit's `anchor` in your output. This hands the unlocatable anchors to a
possible re-plan so it can choose different anchors instead of reissuing the
same edit. When the list is absent or empty (every planned edit was locatable),
record an empty list.

## When to transition

After all edits are applied (and syntax is clean if applicable), transition to
the verify phase.

## Convergence guard — MANDATORY

If you have already read the same file **3 or more times in a row** without
issuing an edit or write op on it, STOP reading and do ONE of:

- Issue the edit or write op you have been preparing (= commit to a change),
  OR
- Transition to verify with the edits completed so far.

Do NOT issue another read on the same file if the previous 2 turns were also
reads of that file.  Re-reading accumulates results in the context without
advancing the plan.  The accumulated read results will NOT change — the file
content is fixed until an edit op is issued.

Similarly, if you have attempted the same command (same argv) **3 or more
consecutive times** and it keeps failing with the same error, STOP and
transition rather than retrying.  Repeated command failures with identical
error output indicate a structural problem that additional retries will not
resolve within this budget.
