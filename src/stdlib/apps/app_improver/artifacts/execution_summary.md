---
type: artifact
name: execution_summary
---

# Summary of the target app's execution run.

app_dsl_path: string
  # Path to the target app DSL that was run.

success: boolean
  # True if the run completed without a fatal error (status == "finished").

final_output: object
  # The target app's final output data.

events_glob: string
  # Glob pattern to find the events JSONL file within the meta workspace.
  # e.g. "invoke/my_app/runs/*.jsonl"

artifacts_glob: string
  # Glob pattern to find artifact JSON files within the meta workspace.
  # e.g. "invoke/my_app/artifacts/**/*.json"
