---
type: phase
name: apply
input: plan
role: implementer
model_class: standard
allowed_ops: [file, shell]
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

Collect the repository-relative paths of all files that were modified.  Carry
`test_patch` from the input so the verify phase can access it.

## When to transition

After all edits are applied (and syntax is clean if applicable), transition to
the verify phase.
