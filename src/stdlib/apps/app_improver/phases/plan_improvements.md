---
type: phase
name: plan_improvements
input: execution_report
input_description: |
  Execution analysis report: phase_reports, artifact_reports, issues, strengths,
  quality_score, improvement_areas, and app_dsl_path.
role: app_architect
---

Design concrete, targeted DSL file changes to address the issues identified in the execution report.

For each issue in execution_report.issues, determine the specific DSL file change that addresses it.
Read the current content of each DSL file before proposing changes (use file read ops).

Change types and when to use them:
- `update_instructions`: phase instructions are unclear, incomplete, or missing domain constraints
  → Rewrite the phase .md body with sharper, more specific instructions
- `update_artifact_schema`: artifact fields are missing, poorly typed, or misnamed
  → Update the artifact .md with corrected field definitions
- `restructure_phase`: the phase role, input artifact, or overall design is wrong
  → Rewrite the entire phase .md including frontmatter
- `add_file`: a new phase or artifact is needed (e.g. adding a review phase that was missing)
  → Provide the full file content

Rules for generating `new_content`:
- For phase files: preserve the frontmatter format (--- delimited YAML) and follow the existing style
- For artifact files: follow the schema format used in the existing artifacts
- Make changes minimal and targeted — do not rewrite files that don't need changing
- Each change must reference a specific issue from execution_report.issues in its rationale

Do NOT change files if quality_score >= 8 and no critical issues were found — output an empty changes array and explain in summary.
