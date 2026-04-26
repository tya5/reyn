---
type: phase
name: build_app
input: app_plan
input_description: Structured app plan produced by plan_app. Contains app_name, app_path, entry_phase, finish_criteria, phases (array of phase definitions), transitions, artifacts, and final_output.
role: dsl_writer
can_finish: true
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

phase file (write to {app_path}/phases/{phase_name}.md):
```
---
type: phase
name: {phase_name}
input: {input_artifact}
input_description: {input_description}
role: {role}
can_finish: true
---

{instructions text verbatim}
```
Omit `can_finish` line if the phase cannot finish.

artifact file (write to {app_path}/artifacts/{artifact_name}.md):
```
---
type: artifact
name: {artifact_name}
---

{field_name}: {type}
{field_name}: {type}
```

IMPORTANT: Write ALL artifact files — including the final_output artifact.
Checklist before finishing:
- app.md written
- one phase file per phase in data.phases
- one artifact file per artifact in data.artifacts
- one artifact file for data.final_output (using data.final_output.name as filename)

Write all files using one op per file. After writing, output a decide turn reporting the files written.
