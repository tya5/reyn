---
type: artifact
name: run_params
---

# Parameters for running the target app under observation.

app_dsl_path: string
  # Workspace-relative or project-relative path to the target app's app.md.
  # e.g. "dsl/apps/architecture_analyzer/app.md"

dsl_root: string
  # DSL root directory for the target app.
  # e.g. "dsl/"

test_input: string
  # The --input value to pass to the target app run.

target_workspace: string
  # Workspace path for the target run, relative to project root.
  # e.g. "workspace/target_runs/architecture_analyzer"
  # Must be inside the meta app's workspace so results are readable.

model: string
  # LiteLLM model name to use for the target run.

improvement_focus: string
  # Optional: area to focus improvements on.
  # e.g. "instruction clarity", "artifact schema completeness", "review phase quality"
