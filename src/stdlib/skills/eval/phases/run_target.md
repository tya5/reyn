---
type: phase
name: run_target
input: eval_case_input
role: test_runner
max_act_turns: 2
allowed_ops: [run_skill]
---

Run the target skill with the test case input and build evaluation requests from the resulting phase artifacts.

## Step 1 — Prepare input artifact

- If `case_input` looks like a valid JSON object (starts with `{`), parse it and use as-is.
- Otherwise wrap it: `{"type": "user_message", "data": {"text": case_input}}`

## Step 2 — Run the target skill

Issue a `run_skill` Control IR op:

```json
{
  "kind": "run_skill",
  "skill": "<target_skill_path>",
  "input": "<prepared input>",
  "workspace": "isolated"
}
```

If the run_skill op returns `status` other than `"finished"` (e.g. `"error"`, `"aborted"`, `"loop_limit_exceeded"`), do NOT retry — the failure is structural, not flaky. Skip Step 3 and produce a `case_run_result` with `run_status` set to the returned status, `eval_requests: []`, and proceed to the decide turn. The eval phase will mark the case as failed.

CRITICAL: NEVER abort the eval workflow, regardless of what error the target skill produced. A target skill failure is an expected outcome — always proceed to `evaluate` with a `case_run_result` artifact.

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
