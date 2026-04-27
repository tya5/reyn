---
type: artifact
name: improvement_plan
---

# Structured plan of DSL file changes to improve the target app.

app_dsl_path: string

summary: string
  # One paragraph summarizing the overall improvement strategy.

changes:
  type: array
  items:
    type: object
    properties:
      file:
        type: string
        # Project-relative path to the DSL file to modify.
        # e.g. "dsl/apps/architecture_analyzer/phases/analyze_code.md"
      change_type:
        type: string
        # "update_instructions" | "update_artifact_schema" | "restructure_phase" | "add_file"
      rationale:
        type: string
        # Why this change improves the app, referencing specific evidence from the execution report.
      new_content:
        type: string
        # The complete new file content (for update_* and add_file).
        # For restructure_phase: the new phase .md content.
    required: [file, change_type, rationale, new_content]
