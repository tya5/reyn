---
type: phase
name: apply_improvements
input: improvement_plan
input_description: |
  Improvement plan: app_dsl_path, summary, and changes array. Each change has
  file (project-relative path), change_type, rationale, and new_content.
role: implementer
can_finish: true
---

Apply each planned change by writing the new file content to disk.

For each entry in improvement_plan.changes:
1. Read the current file content first (to confirm it exists and understand the baseline).
2. Write the new_content to the file path specified in `file`.
   - File paths are project-relative (e.g. "reyn/project/arch_analyzer/phases/analyze.md").
   - Write using a workspace-relative path: strip any leading "reyn/" or project prefix if needed,
     but since write ops are workspace-relative and the DSL is outside the workspace, you must
     write to the path as given using an absolute-style write — note: writes are workspace-restricted.
     IMPORTANT: DSL files are at project-level paths, NOT inside the workspace.
     Use file write ops with paths relative to the workspace for workspace files ONLY.
     For project DSL files, write them as workspace files under a "dsl_patches/" directory,
     then report the intended target path in files_modified so the user can apply them.

IMPORTANT — write boundary:
Writes are restricted to the workspace. DSL source files (reyn/project/...) are outside the workspace.
Therefore: write each improved file to "dsl_patches/{original_relative_path}" inside the workspace.
Example: improvement for "reyn/project/arch_analyzer/phases/analyze.md"
  → write to "dsl_patches/apps/arch_analyzer/phases/analyze.md"
  → report files_modified as ["dsl_patches/apps/arch_analyzer/phases/analyze.md → reyn/project/arch_analyzer/phases/analyze.md"]

In next_steps, tell the user: "Review the patched files in workspace/dsl_patches/ and copy them to their target paths to apply the improvements."

If improvement_plan.changes is empty, set files_modified=[] and explain in summary that no changes were needed.
