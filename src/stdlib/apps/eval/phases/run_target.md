---
type: phase
name: run_target
input: eval_case_input
role: test_runner
---

Run the target app with the test case input and build evaluation requests from the resulting phase artifacts.

## Step 1 — Prepare input artifact

- If `case_input` looks like a valid JSON object (starts with `{`), parse it and use as-is.
- Otherwise wrap it: `{"type": "user_message", "data": {"text": case_input}}`

## Step 2 — Run the target app

Issue a `run_app` Control IR op:

```json
{
  "kind": "run_app",
  "app": "<target_app_path>",
  "input": "<prepared input>",
  "workspace": "isolated"
}
```

## Step 3 — Build eval_requests

The result includes `phase_artifacts` — a list of `{phase, artifact, path}` entries for every intermediate phase output collected during the run.

For each entry in `phase_criteria` (in order), find the matching entry in `phase_artifacts` where `entry.phase == phase_criteria_item.phase_name`. If a phase was visited more than once, use the last matching entry.

For each match, build a phase_eval_request item:

```json
{
  "type": "phase_eval_request",
  "data": {
    "phase_name": "<phase_name>",
    "artifact_type": "<entry.artifact.type>",
    "artifact_path": "<entry.path>",
    "criteria": "<phase_criteria_item.criteria>"
  }
}
```

Use `entry.path` (the CWD-relative file path returned in `phase_artifacts`) as `artifact_path`. Do NOT inline `entry.artifact.data` — the judge reads the file directly.

Skip any phase in `phase_criteria` that has no matching entry in `phase_artifacts`.

## Step 4 — Produce case_run_result

Output:
- `case_name`: from input
- `spec_path`: from input
- `run_status`: from `result.status`
- `eval_requests`: the list built in Step 3, in `phase_criteria` order
