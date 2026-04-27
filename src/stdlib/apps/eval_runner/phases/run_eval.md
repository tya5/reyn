---
type: phase
name: run_eval
input: eval_request | user_message
input_description: |
  Either a structured eval_request (spec_path: CWD-relative or workspace-relative path to eval.md,
  model: LiteLLM model string, app_name: optional hint) or a user_message describing what to evaluate.
  If spec_path or model is missing, use ask_user.
role: validator
can_finish: true
---

Run `agent-os eval` against the target eval spec and report results.

## Step 1 — Resolve spec_path

Extract `spec_path` from input (e.g. `"eval_specs/my_app/eval.md"` or `"workspace/run/eval_specs/my_app/eval.md"`).

Try to run the eval directly first:
```
agent-os eval --spec {spec_path} --model {model} 2>&1; echo "EXIT:$?"
```

If the command fails with "No such file or directory" or "not found", the path is likely
workspace-relative. Locate the actual eval.md with:
```
find . -name "eval.md" -path "*/eval_specs/*" -not -path "*/src/*" 2>/dev/null | head -5
```
If multiple matches, prefer the one whose path contains app_name (hint from eval_request).
Re-run eval with the resolved path.

## Step 2 — Parse output

`agent-os eval` prints a summary line:
```
 Overall: [████████████████████] 1.00  (12/12)
 Weakest phase: analyze
```
And exits with:
- 0 = all cases finished and score >= 0.6
- 2 = score < 0.6 (partial pass)
- 1 = hard error (app failed to run)

Extract from stdout:
- `overall_score`: float from the Overall line (e.g. `1.00`)
- `passed_criteria` / `total_criteria`: integers from `(N/M)` in the Overall line
- `weakest_phase`: from "Weakest phase:" line, or empty string if absent

## Step 3 — Set output fields

- `passed`: true if exit code is 0 (score >= 0.6 and no hard errors)
- `overall_score`: parsed from stdout; 0.0 if hard error
- `passed_criteria` / `total_criteria`: parsed counts; 0 if hard error
- `weakest_phase`: parsed from stdout; empty string if not present
- `spec_path`: the resolved path actually evaluated
- `summary`: one sentence — e.g. "All 12 criteria passed (score 1.00)." or "6/12 criteria passed (score 0.50) — weakest phase: analyze."
