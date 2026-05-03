# Postprocessor

A **postprocessor** is a deterministic transformation that runs at skill
finish, between the LLM's final output and the artifact that the caller
receives.

It mirrors the [phase preprocessor](principles.md) structurally — same
step types, same op set, same `on_error` semantics, same permission
gate. The only difference is **fire position**: preprocessor fires at
phase entry, postprocessor fires at skill finish.

## Why

Some skills produce a "rich" caller-facing artifact whose computation
mixes LLM-decided fields with deterministic-derivable fields. Examples:

- A blog-writer skill produces `{title, body}` from the LLM, then
  computes `html_rendered`, `word_count`, `reading_minutes`
  deterministically.
- A code-review skill produces `{severity, summary, suggestions}` from
  the LLM, then enriches with `affected_files`, `tagged_owners`
  resolved from the workspace.
- A summariser produces `{paragraphs}` from the LLM, then sanitises
  PII tokens before returning.

In all three cases, the LLM should not waste tokens computing
deterministic fields, and the work should not require a follow-up
phase. Postprocessor is the right home.

## Two output schemas

When a skill has a postprocessor, the skill carries **two** output
schemas:

| Schema | Role | Declared in |
|---|---|---|
| `output_schema` (existing) | LLM's finish contract — what the LLM produces | skill.md frontmatter |
| `postprocessor.output_schema` (new) | Caller's contract — what the skill returns to its invoker | inside the postprocessor block |

The pipeline reads:

```
LLM finish artifact (output_schema-conformant)
        ↓
[postprocessor steps]
        ↓
Caller artifact (postprocessor.output_schema-conformant)
        ↓
Returned to caller
```

Skills without a postprocessor have only the existing `output_schema`,
which serves both contracts (= LLM's contract = caller's contract).

## Symmetry with preprocessor

| | preprocessor | postprocessor |
|---|---|---|
| Fires at | phase entry | skill finish |
| Input source | upstream phase's output (any) | LLM's finish artifact (skill `output_schema`) |
| Output target | phase's `input_schema` (fixed) | postprocessor's `output_schema` (fixed) |
| Step types | `validate` / `run_op` / `iterate` / `lint_plan` / `python` | identical |
| Executable ops | `run_skill` allowed; `ask_user` forbidden; no LLM step | identical (parity) |
| `on_error` policy | `fail` / `skip` / `empty` per step | identical (parity) |
| Permission gate | `skill.permissions` | identical (same skill-level decl) |

The runner shares logic with preprocessor — the only differences are
which artifact flows in, which schema validates the output, and the
fire site.

## Declaration

```yaml
---
name: blog_writer
entry: draft
graph:
  draft: [review]
final_output: post                      # LLM contract (existing)
postprocessor:                          # caller contract (new)
  output_schema: rendered_post          # artifact-name reference
  steps:
    - type: python
      module: ./rendering.py
      function: to_html
    - type: python
      module: ./rendering.py
      function: count_words
    - type: validate
      schema:
        type: object
        properties:
          word_count: { type: integer, minimum: 1 }
        required: [word_count]
---
```

`output_schema` accepts either a dict literal (inline JSON Schema) or
a string referencing an artifact name in the skill's artifact
registry. The artifact-name form is preferred for stdlib reuse.

## Failure semantics

A postprocessor step can declare `on_error: fail | skip | empty`,
identical to preprocessor:

- **`fail`** (default): the step's failure raises and aborts the skill.
  The skill abort is recorded as a `WorkflowAbortedError`; per
  [ADR-0013](../decisions/0013-exception-aware-crash-lifecycle.md), the
  per-skill snapshot is deleted (no auto-resume).
- **`skip`**: the step's failure is logged and skipped; subsequent
  steps continue.
- **`empty`**: the step's failure produces an empty result for the
  step's `into:` target; subsequent steps continue.

Use `skip` / `empty` for steps whose failure is recoverable in
context (e.g. an enrichment that's nice-to-have but not critical).
Default to `fail` so the caller never receives a malformed artifact.

## Resume

Postprocessor steps run through the same `dispatch_tool` as preprocessor
and phase ops, so they emit `step_completed` events and participate in
memoization. A crash mid-postprocessor:

1. Per-skill snapshot has `current_phase = "__post__"` (reserved
   pseudo-phase).
2. Auto-resume reads the snapshot, jumps directly to postprocessor
   replay, and skips already-committed steps via memo lookup.
3. World-purity ops (= `file/read`, MCP read APIs) re-execute on resume
   per [ADR-0011](../decisions/0011-world-purity-memo-invalidation.md).

The LLM's finish artifact is persisted to workspace before
postprocessor starts so resume has the durable input artifact even if
the in-process state was lost.

## When to use a postprocessor vs a follow-up phase

Use a **postprocessor** when:

- The transformation is purely deterministic (no LLM call, no
  user input).
- The output is mechanically derivable from the LLM's finish artifact.
- You don't need to expose the intermediate state to the LLM.

Use a **follow-up phase** when:

- The next step requires LLM judgement.
- The transformation could fail in interesting ways the LLM should
  retry / explain.
- You want the next step's outputs validated by a phase's normal
  schema check.

Postprocessor is for "polish" / "rendering" / "validation" / "metric
emission" type work that's expensive in LLM tokens but cheap in
deterministic code.

## Out of scope (deferred)

- Postprocessor with `retry` semantics (= step failure triggers re-LLM
  call). Not supported; if you need re-LLM call on validation
  failure, model it as a phase with retry policy.
- Postprocessor as a standalone hook outside skills. Postprocessor
  belongs to a skill's contract.
- Phase-level postprocessor (= "phase exit" hook). Not needed: the
  next phase's preprocessor already covers phase-boundary
  transformations.

## See also

- [Phase vs Skill vs OS](phase-vs-skill-vs-os.md) — the architectural
  layer postprocessor lives in.
- [Permission model](permission-model.md) — what `skill.permissions`
  governs.
- [Skill resume](skill-resume.md) — the broader resume machinery
  postprocessor integrates with.
