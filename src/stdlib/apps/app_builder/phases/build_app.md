---
type: phase
name: build_app
input: app_plan
role: dsl_writer
---

Generate DSL markdown files for the app defined in data, then write each one to the workspace using file ops.

CRITICAL: Every file MUST start with `---` and end the frontmatter block with `---`. Missing delimiters will break the parser.

app.md (write to {app_path}/app.md):
```
---
type: app
name: {app_name}
description: {app_description}
entry: {entry_phase}
final_output: {final_output.name}
final_output_description: {final_output.description}
finish_criteria:
  - {criterion1}
  - {criterion2}
graph:
  {phase_a}: [{phase_b}]
  {phase_b}: [{phase_c}, {phase_d}]
---

## 概要
{app_descriptionの散文説明}

## 入力
{入力に期待する内容と例}
```

graph comes from data.transitions. Each `{from: X, to: [Y, Z]}` entry becomes:
```yaml
X: [Y, Z]
```
Review loops look like:
```yaml
graph:
  generate: [review]
  review: [generate, deliver]
```

CRITICAL — no "finish" node: Do NOT add a `finish` node to the graph.
Workflow termination is expressed by `can_finish: true` on the phase that delivers the final output.

CRITICAL — skip edges to final_output: If any transition target equals data.final_output.name, OMIT that edge.
The final_output artifact is NOT a phase. Only emit edges where the target is a phase listed in data.phases.

CRITICAL — graph must be a DAG (no cycles): Do NOT write back-edges (e.g. review → generate).
Review/revise loops are handled by OS rollback — the review phase emits control.type="rollback" at runtime.
The graph only expresses forward flow. Any cycle will fail the linter.

CRITICAL — do NOT write a user_message artifact file: `user_message` is a stdlib artifact.
If the entry phase accepts `user_message` as input, simply reference it in the phase frontmatter — do not create an artifact file for it.

phase file (write to {app_path}/phases/{phase_name}.md):
```
---
type: phase
name: {phase_name}
input: {input_artifact}
role: {role}
model_class: {model_class}
can_finish: true
---

{instructions text verbatim}
```
Omit `can_finish` line if the phase cannot finish.
Omit `model_class` line if the phase should use the runtime default (standard).

artifact file (write to {app_path}/artifacts/{artifact_name}.yaml):
```
name: {artifact_name}
description: {artifact_description}
schema:
  type: object
  properties:
    {field_name}:
      type: {json_schema_type}
      description: {field_description}
    {field_name}:
      type: array
      items:
        type: string
      description: {field_description}
  required: [{required_field1}, {required_field2}]
```
Always include `type: object`, `properties`, `required`, and a `description` on every field.
Use the schema exactly as defined in data.artifacts[].schema and data.final_output.schema.
Artifact files are plain YAML — no frontmatter delimiters.

IMPORTANT: Write ALL artifact files — including the final_output artifact.
Checklist before finishing:
- app.md written
- one phase file per phase in data.phases
- one artifact file per artifact in data.artifacts (all fields have descriptions and explicit types)
- one artifact file for data.final_output (using data.final_output.name as filename)
- every phase's `input:` field resolves to either a written artifact file, `user_message` (stdlib), or data.final_output.name — if any phase's input is missing, STOP and write the missing artifact file before proceeding

Write all files using one op per file. After writing, run the linter using the app_path from input data:

```json
{"kind": "lint", "app_path": "<data.app_path>"}
```

Example: if data.app_path is "reyn/local/article_generator", emit `{"kind": "lint", "app_path": "reyn/local/article_generator"}`.

If lint returns `passed: false`:
- Examine the `issues` list carefully
- If the root cause is a mistake in the generated files (e.g. back-edge in graph, missing artifact): fix the files and re-run lint
- If the root cause is in the input `app_plan` (e.g. the plan itself defines a cycle in transitions): emit `control.type="rollback"` with `reason` explaining which part of the plan is invalid — the OS will re-run the planning phase with your feedback
- Do NOT finish if lint has errors

If lint passes, finish with an `app_builder_result` artifact:
- `app_name`: the generated app name
- `app_path`: workspace-relative path (e.g. "reyn/local/my_app")
- `files_written`: list of all file paths written
- `file_count`: total number of files
- `lint_passed`: true
- `lint_issues`: []
- `summary`: one sentence describing what the app does for its users

summary MUST describe what the app does for its users — not what you (the builder) did.
Good: "An app that lets users submit documents for reviewer approval or rejection with reasons."
Bad: "Generated DSL files for the review app and saved them to the workspace."
