---
type: phase
name: run_target
input: run_params
input_description: |
  Run parameters for the target app: app_dsl_path, dsl_root, test_input,
  target_workspace, model, and improvement_focus.
role: executor
---

Execute the target app as a subprocess and capture the results.

Build and run this shell command:
```
agent-os run --app-dsl {app_dsl_path} --dsl-root {dsl_root} --input '{test_input}' --workspace {target_workspace} --model {model}
```

Use the shell Control IR op with timeout=300 (target apps may take several minutes).

After execution, inspect stdout and stderr to determine:
- `exit_code`: the returncode from the shell result
- `success`: true if returncode is 0 or 2 (2 = workflow ended with warning, not a fatal error)
- `stdout` / `stderr`: copy from shell result

From the stdout, the events file path is printed as: `events saved → {path}`
Strip the leading `workspace/` prefix from that path to get the workspace-relative events path.
Derive `events_glob` as: the directory of that workspace-relative path + "/*.jsonl"
  e.g. if events path is "target_runs/arch_analyzer/runs/20260426T....jsonl"
  then events_glob = "target_runs/arch_analyzer/runs/*.jsonl"

Set `artifacts_glob` to: "{target_workspace_rel}/artifacts/**/*.json"
  where target_workspace_rel strips the leading "workspace/" from target_workspace.
  e.g. "target_runs/arch_analyzer/artifacts/**/*.json"

If the shell op is not available (status=skipped), set success=false and stderr="shell op not enabled — run with --allow-shell".
