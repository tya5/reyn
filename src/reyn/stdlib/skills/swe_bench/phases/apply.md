---
type: phase
name: apply
input: plan
role: implementer
model_class: standard
allowed_ops: [file, shell]
max_act_turns: 30
---

Implement the edit plan by modifying the repository files.

## Step 1 — Read each file before editing

For every file listed in the plan's edits, issue a file read op to retrieve
the current content.  Do NOT edit from memory — always read first to ensure
the edit target is accurate.

## Step 2 — Apply each edit

For each entry in the plan, issue the appropriate file op:

- Use an edit op for targeted replacements within an existing file
- Use a write op only when creating a new file or replacing the entire content
  of a file

Apply edits in the order they appear in the plan.  After each edit, confirm
that the op succeeded before proceeding to the next.

## Step 3 — Basic syntax check (when applicable)

If the edited files are Python, issue a shell op running
`python -m py_compile <file>` on each modified file.  If a compile error is
reported, correct the syntax before transitioning.

For non-Python files, skip this step.

## Step 4 — Record what was changed

Collect the repository-relative paths of all files that were modified.

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

Similarly, if you have attempted the same shell command (same command string)
**3 or more consecutive times** and it keeps failing with the same error,
STOP and transition rather than retrying.  Repeated shell failures with
identical error output indicate a structural problem that additional retries
will not resolve within this budget.
