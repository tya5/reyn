---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [skill.md]
---

# Postprocessor

A Skill may declare a `postprocessor` block that runs **after** the LLM emits its finish artifact, before that artifact is returned to the caller. Steps are deterministic: they invoke sub-skills, iterate over a list, run validators, lint a plan, or call a Python function. The caller receives the postprocessor's output, not the raw LLM artifact.

For rationale and worked examples of **why** to use a postprocessor, see [Concepts: postprocessor](../../concepts/postprocessor.md).

## Block location

`postprocessor:` is declared in `skill.md` frontmatter, as a sibling to `final_output:`, `graph:`, and `permissions:`:

```yaml
---
type: skill
name: blog_writer
entry: draft
final_output: post            # LLM contract — what the LLM must produce
graph:
  draft: [review]
  review: [end]
permissions:
  python:
    - module: rendering
      function: to_html
      mode: safe
    - module: rendering
      function: count_words
      mode: safe
postprocessor:
  output_schema: rendered_post  # caller contract — what the skill returns
  output_description: |
    Fully rendered HTML post with word count and reading time.
  steps:
    - type: python
      module: rendering
      function: to_html
      into: html_body
    - type: python
      module: rendering
      function: count_words
      into: word_count
---
```

## Required fields

### `output_schema`

Declares the schema of the artifact the postprocessor produces. Accepts either:

- **Artifact-name string** — references an artifact defined in the skill's `artifacts/` directory or in stdlib. Preferred for reuse across skills.

  ```yaml
  postprocessor:
    output_schema: rendered_post
  ```

- **Inline dict** — a JSON Schema dict literal declared directly in the frontmatter.

  ```yaml
  postprocessor:
    output_schema:
      type: object
      required: [html_body, word_count]
      properties:
        html_body:   { type: string }
        word_count:  { type: integer, minimum: 1 }
  ```

The OS validates the postprocessor's output against this schema. Validation failure triggers the `on_error` policy of the failing step (or aborts the skill if no policy is set).

## Optional fields

### `output_name`

A short identifier for the produced artifact, used in event payloads and log lines. Defaults to the skill name with a `_post` suffix if omitted.

```yaml
postprocessor:
  output_schema: rendered_post
  output_name: rendered_post
```

### `output_description`

Long-form description of the postprocessor's output. Shown by `reyn skills <name>` alongside the skill's prose body.

### `steps`

An ordered list of deterministic steps. Steps run sequentially; each can read the LLM finish artifact and any `into` keys produced by earlier steps. The same step kinds as the preprocessor are supported — see [preprocessor.md](preprocessor.md) for syntax details on each:

| `type` | Purpose |
|--------|---------|
| `run_skill` | Invoke a sub-skill, store its output under a named key |
| `iterate` | Fan a sub-step out over a list, collect results |
| `validate` | Run a JSON-Schema check against the accumulated artifact |
| `lint_plan` | Run deterministic structural checks on a plan artifact |
| `python` | Call a user-supplied Python function (sandboxed) |

If `steps` is omitted, the postprocessor acts as a validate-only transformation: the LLM artifact is validated against `output_schema` and returned as-is on success.

## `on_error` policy

Each step may declare `on_error: fail | skip | empty`. The semantics are identical to the preprocessor:

| Value | Behaviour |
|-------|-----------|
| `fail` (default) | Step failure raises and aborts the skill. The abort is a `WorkflowAbortedError`; the per-skill snapshot is deleted (no auto-resume). |
| `skip` | Step failure is logged; subsequent steps continue without the step's `into` key. |
| `empty` | Step failure produces an empty result at the step's `into` key; subsequent steps continue. |

Default to `fail` so the caller never receives a malformed artifact. Use `skip` or `empty` only for enrichments that are nice-to-have but not critical to the caller's contract.

## Executable op set and permissions

The executable op set mirrors the preprocessor exactly:

- `run_skill` is allowed.
- `ask_user` is **forbidden** (skill finish is caller-synchronous; user interaction at this point is undefined).
- No LLM step — postprocessor is deterministic by definition.

See [preprocessor.md](preprocessor.md) for the full op-set discussion.

Permission enforcement uses `skill.permissions` — the skill-level declaration in `skill.md` frontmatter. There is no phase-level permission gate for postprocessor steps. See [permission-model.md](../../concepts/permission-model.md) for semantics.

## Resume integration

Postprocessor steps run through the same `dispatch_tool` as preprocessor and phase ops. Each step emits a `step_completed` event and participates in memoization. On crash mid-postprocessor:

1. The per-skill snapshot records `current_phase = "__post__"` (reserved pseudo-phase).
2. Auto-resume replays the postprocessor from the first uncommitted step, skipping already-committed steps via memo lookup.
3. World-purity ops re-execute on resume.

The LLM's finish artifact is persisted to workspace before postprocessor starts, so resume has a durable input artifact regardless of in-process state. Op invocation IDs for postprocessor steps follow the pattern `__post__.<step_idx>` (e.g. `__post__.0`, `__post__.1`). See [skill-resume.md](../../concepts/skill-resume.md) for the broader resume machinery.

## Worked examples

### 1. Inline `output_schema` with python enrichment

```yaml
postprocessor:
  output_schema:
    type: object
    required: [title, body, word_count]
    properties:
      title:       { type: string }
      body:        { type: string }
      word_count:  { type: integer, minimum: 1 }
  output_description: Draft post enriched with word count.
  steps:
    - type: python
      module: stats
      function: count_words
      mode: safe
      output_schema:
        type: object
        required: [word_count]
        properties:
          word_count: { type: integer }
      into: word_count
```

The `stats.py` function receives the LLM's finish artifact and returns `{ word_count: 847 }`. The OS merges `word_count` into the artifact and validates against `output_schema`.

Requires a matching `permissions.python` entry:

```yaml
permissions:
  python:
    - module: stats
      function: count_words
      mode: safe
```

### 2. Artifact-name reference for `output_schema`

```yaml
postprocessor:
  output_schema: code_review_enriched   # defined in artifacts/code_review_enriched.yaml
  steps:
    - type: run_skill
      skill: resolve_owners
      input:
        type: affected_files_list
        data: { files: "${artifact.affected_files}" }
      into: tagged_owners
```

The artifact-name form delegates schema ownership to `artifacts/code_review_enriched.yaml`, where it can be versioned and reused across skills.

### 3. Validate-only postprocessor (no steps)

```yaml
postprocessor:
  output_schema:
    type: object
    required: [summary, severity]
    properties:
      summary:  { type: string, minLength: 10 }
      severity: { type: string, enum: [low, medium, high, critical] }
```

No `steps` key. The OS validates the LLM's finish artifact against `output_schema` and returns it directly on success. This is the lightest-weight use: enforcing a stricter shape than `final_output` without any transformation.

## See also

- [Concepts: postprocessor](../../concepts/postprocessor.md) — rationale and when to use
- [preprocessor.md](preprocessor.md) — step types (shared with postprocessor)
- [skill-md.md](skill-md.md) — full skill frontmatter reference
- [permission-model.md](../../concepts/permission-model.md) — `skill.permissions` semantics
- [skill-resume.md](../../concepts/skill-resume.md) — resume machinery postprocessor integrates with
