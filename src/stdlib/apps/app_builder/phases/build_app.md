---
type: phase
name: build_app
input: app_plan
input_description: Structured app plan produced by plan_app. Contains app_name, app_path, entry_phase, finish_criteria, phases (array of phase definitions), transitions, artifacts, and final_output.
role: dsl_writer
---

Generate DSL markdown files for the app defined in data, then write each one to the workspace using file ops.

CRITICAL: Every file MUST start with `---` and end the frontmatter block with `---`. Missing delimiters will break the parser.

app.md (write to {app_path}/app.md):
```
---
type: app
name: {app_name}
entry: {entry_phase}
final_output: {final_output.name}
final_output_description: {final_output.description}
finish_criteria:
  - {criterion1}
  - {criterion2}
---

{phase_a} -> {phase_b}
{phase_b} -> {phase_c}
```

Graph edges come from data.transitions. Each `{from: X, to: [Y, Z]}` entry becomes one line per target:
```
X -> Y
X -> Z
```
Review loops look like:
```
generate -> review
review -> generate
review -> deliver
```

CRITICAL — no "finish" node: Do NOT add a `finish` node to the graph.
Workflow termination is expressed by `can_finish: true` on the phase that delivers the final output.
The phase uses `can_finish: true` in its frontmatter — there is NO separate "finish" phase or edge.

phase file (write to {app_path}/phases/{phase_name}.md):
```
---
type: phase
name: {phase_name}
input: {input_artifact}
input_description: {input_description}
role: {role}
model_class: {model_class}
can_finish: true
---

{instructions text verbatim}
```
Omit `can_finish` line if the phase cannot finish.
Omit `model_class` line if the phase should use the runtime default (standard).

artifact file (write to {app_path}/artifacts/{artifact_name}.md):
```
---
type: artifact
name: {artifact_name}
---

{field_name}: {type}
{field_name}: {type}
```
CRITICAL: Every artifact field MUST have an explicit type — never leave a type blank.
Valid types: string | integer | number | boolean | string[] | integer[] | number[] | array | object
Wrong: `approved:` — Right: `approved: boolean`
If a field truly has no type specified, default to `string`.

IMPORTANT: Write ALL artifact files — including the final_output artifact.
Checklist before finishing:
- app.md written
- one phase file per phase in data.phases
- one artifact file per artifact in data.artifacts (all fields have explicit types)
- one artifact file for data.final_output (using data.final_output.name as filename)
- every phase's `input:` field resolves to either a written artifact file, `user_message` (stdlib), or data.final_output.name — if any phase's input is missing, STOP and write the missing artifact file before proceeding

Write all files using one op per file. After writing, output a decide turn reporting the files written.

summary MUST describe what the app does for its users — not what you (the builder) did.
Good: "An app that lets users submit documents for reviewer approval or rejection with reasons."
Bad: "Generated DSL files for the review app and saved them to the workspace."
