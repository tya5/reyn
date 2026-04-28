---
type: phase
name: run_target
input: run_params
role: executor
---

Execute the target app in-process using the run_app Control IR op.

Build the input artifact from test_input:
- If test_input looks like a JSON object, parse it and use as-is.
- Otherwise wrap it as: {"type": "user_message", "data": {"text": test_input}}

Issue a run_app op:
```
{
  "kind": "run_app",
  "app": app_dsl_path,
  "input": <input artifact>,
  "model": model,
  "workspace": "isolated"
}
```

From the result, populate execution_summary:
- `app_dsl_path`: the app_dsl_path from run_params
- `success`: result.success
- `final_output`: result.final_output
- `events_glob`: result.events_glob
- `artifacts_glob`: result.artifacts_glob
