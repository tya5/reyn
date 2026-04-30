---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [artifacts/*.yaml]
---

# `artifacts/<name>.yaml`

An artifact is a typed structured value passed between phases. Each artifact has a YAML schema in the skill's `artifacts/` directory.

## Minimal example

```yaml
# artifacts/topic_input.yaml
type: object
required: [topic]
properties:
  topic:
    type: string
    description: Subject the skill should write about.
```

## Schema

reyn artifact files are JSON Schema (Draft-7) fragments expressed in YAML.

| Field | Required | Notes |
|-------|----------|-------|
| `type` | yes | Almost always `object`. |
| `required` | optional | List of required property names. |
| `properties` | yes (for objects) | Map of name → JSON Schema. |
| `description` | optional | Free-form description; surfaces in the LLM context. |
| `additionalProperties` | optional | Default: `true`. Set `false` for strict shapes. |

## Strict vs lenient validation

By default, reyn validates only the top level — nested required fields are not enforced. Pass `--strict` to enforce required fields at every nesting depth.

## Cross-skill artifacts (stdlib)

Artifacts under `src/stdlib/artifacts/*.yaml` are available to every skill. The most common is `user_message`:

```yaml
# src/stdlib/artifacts/user_message.yaml
type: object
required: [text]
properties:
  text:
    type: string
    description: Free-text user input.
```

Skills that accept natural-language input declare `input: user_message | <other_artifact>` in their entry phase.

## Naming conventions

- Files: `lowercase_snake_case.yaml`.
- Type: filename without `.yaml` is the artifact's type name.
- Properties: `lowercase_snake_case`.
- Avoid kitchen-sink artifacts; if a property is needed only for one phase, put it in that phase's input artifact.

## Validation in the runtime

- On transition: the LLM's `artifact.data` is validated against the next phase's input schema.
- On finish: validated against the skill's `final_output_schema`.
- Failures emit `validation_error` events and re-prompt (subject to retry limits).

## See also

- [skill-md.md](skill-md.md) — `final_output` references an artifact name
- [phase-md.md](phase-md.md) — phase `input` references one or more artifact names
- [graph.md](graph.md) — graph + artifact resolution
