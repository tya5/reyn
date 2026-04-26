---
type: phase
name: build_app
input: app_plan
input_description: Structured app plan produced by plan_app. Contains app_name, app_path, entry_phase, finish_criteria, phases (array of phase definitions), transitions, artifacts, and final_output.
role: dsl_writer
can_finish: true
---

Generate DSL markdown files for the app defined in data, then write each one to the workspace using control_ir file ops.

DSL format rules:

app.md (write to {app_path}/app.md):
  Frontmatter fields: type: app, name, entry (entry_phase value), final_output, final_output_description, finish_criteria (YAML list)
  Body: one transition per line as "phase_a -> phase_b"

phase files (write to {app_path}/phases/{phase_name}.md):
  Frontmatter fields: type: phase, name, input (input_artifact value), input_description, role, can_finish (only if true)
  Body: the instructions text verbatim

artifact files (write to {app_path}/artifacts/{artifact_name}.md):
  Frontmatter fields: type: artifact, name
  Body: one field per line as "field_name: type"

final_output artifact (write to {app_path}/artifacts/{final_output.name}.md):
  Same format as other artifact files

Write all files in a single response using one control_ir op per file.
After writing, report the list of file paths written.
