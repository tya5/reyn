---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [phases/*.md]
---

# Preprocessor

A Phase may declare a `preprocessor` chain that runs **before** the LLM is called. Steps are deterministic: they invoke sub-skills, iterate over a list, run validators, lint a plan, or call a Python function. The LLM sees an enriched input artifact whose schema is inferred at compile time.

## Step kinds

| `type` | Purpose |
|--------|---------|
| `run_skill` | Invoke a sub-skill, store its output under a named key |
| `iterate` | Fan a sub-step out over a list, collect results |
| `validate` | Run a JSON-Schema check, surface findings to the LLM |
| `lint_plan` | Run deterministic structural checks on a plan artifact |
| `python` | Call a user-supplied Python function (sandboxed) |

All steps share two common ideas:

- The result is placed into the input artifact at a key named by `into`.
- Steps run in order; each can read what previous steps produced.

## `run_skill`

```yaml
preprocessor:
  - run_skill:
      skill: recall_memory
      input:
        type: user_message
        data: { text: "what does the user prefer?" }
      into: relevant_memories
```

The named sub-skill runs to completion; its `final_output` artifact is stored at `input.relevant_memories`.

## `iterate`

```yaml
preprocessor:
  - iterate:
      over: phase_eval_requests          # dot-path to an array in the input
      apply:
        run_skill:
          skill: judge_phase
          input: { type: phase_eval_request, data: ${item} }
      into: phase_judgments
      on_error: fail                     # or "skip"
```

Each item in `over` triggers `apply`. Results are collected into `into` as a list. MVP supports `run_skill` as the inner step.

## `validate`

```yaml
preprocessor:
  - validate:
      schema:
        type: object
        required: [topic]
        properties:
          topic: { type: string }
      target: input
      into: validation_findings
```

Runs JSON Schema validation against a target slice of the input. Findings (errors and warnings) are placed at `into`. The LLM can then decide how to respond.

## `lint_plan`

Runs deterministic structural checks (cycle detection, artifact coverage) on a plan artifact. Used by `skill_builder`'s `review_plan` phase.

## `python`

```yaml
preprocessor:
  - python:
      module: stats
      function: compute
      mode: safe                         # safe | unsafe
      output_schema:
        type: object
        required: [word_count]
        properties:
          word_count: { type: integer }
      into: stats
```

Invokes `<skill_dir>/<module>.py:<function>(artifact)` and stores the JSON-serializable result at `into`.

### Mode: `safe`

- AST-validated by reyn before execution: bans `open`, `eval`, `exec`, `__import__`, `compile`, `globals`, `locals`, plus `subprocess` and other risky modules.
- Imports limited to a curated allowlist (`math`, `statistics`, `json`, `re`, `random`, `time`, `datetime`, ...). Project may extend via `reyn.yaml`'s `permissions.python.allowed_modules`.
- Restricted `__builtins__`.
- Run in a subprocess for crash isolation and timeout.

### Mode: `unsafe`

- No AST validation. Free Python.
- Requires both `--allow-untrusted-python` AND a `python.unsafe: allow` permission grant.
- Use only for steps that require capabilities the safe mode disallows (file I/O, custom packages).

### `output_schema`

Required. The LLM-visible enrichment shape — declared explicitly because we won't run un-sandboxed user code at compile time to infer it.

## Common rules

- The `into` key must not collide with an existing input artifact key.
- Step ordering matters: a later step can reference an earlier step's `into` slot.
- Linter checks: each `python` step's module/function must match a `permissions.python` entry, the `.py` file must exist, the function must be defined, and (in safe mode) the AST is validated.

## What phases MUST NOT do

- **Describe preprocessor mechanics in the phase body.** Refer to enriched fields by name only — the LLM doesn't need to know they came from a preprocessor (P8).

## See also

- [phase-md.md](phase-md.md) — Phase frontmatter
- [Reference: permissions](../config/permissions.md) — declaring `python` permissions
- [How-to: add a Python preprocessor](../../guide/for-skill-authors/add-a-python-preprocessor.md)
