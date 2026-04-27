---
type: phase
name: prepare
input: user_message
input_description: |
  Natural language request specifying the target app to improve. Should include:
  the target app DSL path, a test input string, and optionally a focus area.
  Example: "Improve dsl/apps/architecture_analyzer with input 'Analyze this project' — focus on review phase quality."
role: meta_coordinator
---

Parse the user's request and establish the run parameters for the target app.

Extract from the user_message:
- `app_dsl_path`: the target app's app.md path (e.g. "dsl/apps/architecture_analyzer/app.md"). If missing, use ask_user.
- `dsl_root`: infer from app_dsl_path (e.g. "dsl/" if path starts with "dsl/apps/"). Default: "dsl/".
- `test_input`: the test input string to pass to the target app. If missing, use ask_user.
- `model`: LiteLLM model to use. Default to the same model currently running.
- `improvement_focus`: optional focus area from the user's request. Empty string if not specified.

Set `target_workspace` to: "workspace/target_runs/{app_name}" where app_name is the snake_case app name extracted from app_dsl_path.
This places the target run inside the meta app's workspace so its output files are readable.
