---
type: artifact
name: execution_summary
---

# Summary of the target app's execution run.

app_dsl_path: string
  # Path to the target app DSL that was run.

target_workspace: string
  # Workspace path used for the target run (project-relative).

exit_code: integer
  # Process exit code. 0 = finished, 2 = workflow ended with warning, non-zero = error.

success: boolean
  # True if the run completed without a fatal error.

stdout: string
  # Full stdout from the run command.

stderr: string
  # Full stderr from the run command (empty on success).

events_glob: string
  # Glob pattern to find the events JSONL file within the meta workspace.
  # e.g. "target_runs/architecture_analyzer/runs/*.jsonl"

artifacts_glob: string
  # Glob pattern to find artifact JSON files within the meta workspace.
  # e.g. "target_runs/architecture_analyzer/artifacts/**/*.json"
