---
type: phase
name: review_plan
input: skill_plan
role: plan_reviewer
model_class: strong
---

Review the skill plan for schema quality before DSL files are written.
Structural checks (DAG, reachability, artifact coverage) are handled deterministically by the linter after build — focus only on what requires judgement.

## Schema checks

**Top-level type**: every artifact schema must have `type: object`.

**Fields have descriptions**: every property in every schema must have a `description` field.

**Required fields declared**: every schema must have a non-empty `required` array.

**Review artifacts are focused**: schemas for review phases should contain only verdict fields — not content duplicated from the artifact being reviewed.

## Python preprocessor checks

If any phase has a non-empty `preprocessor`, also verify:

- Every step's `module` appears as an entry in the top-level `python_modules`
  array. A reference to a missing module is a structural defect.
- Every step's `function` is a top-level `def` in its module's `source`. Skim
  the source briefly; if the function is absent, flag it.
- Every step has a non-empty `output_schema` with `type: object`, explicit
  `properties`, and `required`. Empty or `{}` schemas are rejected.
- Each module's source obeys the chosen `mode`. For `pure`: no imports
  outside the stdlib allowlist (math, statistics, json, re, datetime,
  hashlib, collections, itertools, functools, copy, dataclasses, random,
  time, etc.); no `open` / `eval` / `exec` / `__import__` / file I/O.
  For `trusted`: anything goes, but the user will need
  `--allow-untrusted-python` at runtime — note this in feedback.

If any of these fail, rollback with the issue list — the fix is upstream
in plan_skill or design_artifacts depending on which field is wrong.

## Output

- If issues found: emit `control.type="rollback"` with `control.reason.summary` listing all problems as a numbered list. artifact may be empty.
- If all clear: transition to `build_skill` with `control.type="transition"` and the `skill_plan` artifact unchanged.
