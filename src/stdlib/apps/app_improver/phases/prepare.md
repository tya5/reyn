---
type: phase
name: prepare
input: user_message
role: meta_coordinator
---

Parse the user's request and establish the run parameters for the target app.

Extract from the user_message:
- `app_dsl_path`: the target app's app.md path (e.g. "reyn/project/architecture_analyzer/app.md"). If missing, use ask_user.
- `dsl_root`: infer from app_dsl_path (e.g. "reyn/" if path starts with "reyn/project/"). Default: "reyn/".
- `test_input`: the test input string to pass to the target app. If missing, use ask_user.
- `model`: model class name or LiteLLM string to use. Default to the same model currently running (from ContextFrame `model` field).
- `improvement_focus`: optional focus area from the user's request. Empty string if not specified.

Set `target_workspace` to: "workspace/target_runs/{app_name}" where app_name is the snake_case app name extracted from app_dsl_path.
This places the target run inside the meta app's workspace so its output files are readable.
