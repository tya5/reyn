---
type: phase
name: prepare
input: user_message | improvement_session
role: meta_coordinator
model_class: standard
---

Validate the user's request and produce a fully-populated `improvement_session` for the loop.
This phase runs ONCE per improver invocation — never re-entered.

## Step 1 — Parse the input

If the input artifact type is `user_message`: extract from the `text` field:
- `target_skill_path` (required) — path to the target app's app.md
- `case_name` (optional) — defaults to the FIRST case in the eval spec
- `max_iterations` (optional) — defaults to `3`
- `score_threshold` (optional) — defaults to `0.85`
- `improvement_focus` (optional) — defaults to empty string
- `model` (optional) — defaults to `"standard"`

If `target_skill_path` is missing, use `ask_user`.

If the input artifact type is `improvement_session`: pass the fields through unchanged but still execute Steps 2–5 (paths, eval.md existence, parsing, workspace state).

## Step 2 — Resolve paths

Set `target_dsl_root`:
- If the input provided one, use it.
- Else default to the parent directory of `target_skill_path` (e.g. `target_skill_path = "reyn/local/my_app/skill.md"` → `target_dsl_root = "reyn/local/my_app"`).

Set `eval_spec_path`:
- If the input provided one, use it.
- Else default to `<target_dsl_root>/eval.md`.

## Step 3 — Ensure eval.md exists

Issue a file read op for `eval_spec_path`.
If the file is found, skip to Step 4.

If the file is NOT found, generate it via the `eval_builder` stdlib app. Issue this Control IR op:

```
{
  "kind": "run_skill",
  "app": "eval_builder",
  "input": {
    "type": "user_message",
    "data": {"text": "Generate an eval.md for <target_skill_path>."}
  },
  "model": session.model,
  "workspace": "isolated"
}
```

After the sub-app finishes, read `eval_spec_path` again to confirm it now exists. If it still does not, abort with `control.type="abort"` and a reason citing eval_builder failure.

## Step 4 — Parse eval.md and pick a case

Parse the eval.md content (markdown). The format is:

```
---
type: eval
skill: <path>
dsl_root: <path>
---

## case: <case_name>
input: "<text>"

### phase: <phase_name>
quality:
- <criterion text>          ← required by default
- [aspirational] <text>     ← optional (required: false)
- [required] <text>         ← explicit required tag
```

Pick the case:
- If `case_name` was provided in Step 1, locate the case with that exact name.
- Else use the FIRST case (the one that appears first in the file). Set `case_name` to its name.

Extract `case_input` from the `input:` line of the chosen case (strip surrounding quotes).

For each `### phase:` block under the chosen case (until the next `## case:` or EOF), build:
```
{
  "phase_name": "<name>",
  "criteria": [
    {"description": "<text>", "required": true|false}
  ]
}
```

A criterion has `required: false` ONLY when it begins with `[aspirational]`. Otherwise `required: true`.

Strip the `[required]` / `[aspirational]` tag prefix from `description` when present.

## Step 5 — Initialize workspace state

Issue a file write op to `improver_state.json` (in the workspace root) with:

```json
{
  "session": <the improvement_session you are about to emit>,
  "iterations": []
}
```

This file accumulates iteration history across the loop, surviving rollback chains.

## Output

Emit `improvement_session` with all required fields populated and choose `transition` → `run_and_eval`.
