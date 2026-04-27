---
type: app
name: app_builder
entry: plan_app
final_output: app_builder_result
final_output_description: |
  Summary of the newly created app: its name, workspace path,
  the list of files written, and a one-sentence description of what it does.
finish_criteria:
  - All DSL files for the app have been written to the workspace
  - The phase graph is valid and complete
  - Each artifact referenced by a phase exists as a file
---

plan_app -> build_app
