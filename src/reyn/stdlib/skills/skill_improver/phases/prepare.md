---
type: phase
name: prepare
input: user_message | improvement_session
role: meta_coordinator
model_class: standard
allowed_ops: [file, ask_user, run_skill]
---

Validate the user's request and produce a fully-populated `improvement_session` for the loop.
This phase runs ONCE per improver invocation — never re-entered.

IMPORTANT: The LLM MUST NOT construct file paths. Extract only the skill name from the user
input. Path resolution is performed by the OS using `resolve_skill_path`. The OS injects
resolved paths into the preprocessor — your only job here is to extract the skill NAME.

## Step 1 — Parse the input

If the input artifact type is `user_message`: extract from the `text` field:
- `target_skill` (required) — the short skill name only (e.g. `"direct_llm"`, `"my_app"`)
  - If the user says `"direct_llm を改善して"`, extract `"direct_llm"`.
  - If the user says a path like `"reyn/local/my_app/skill.md"`, extract only the last
    component before `/skill.md` (i.e. `"my_app"`).
  - DO NOT produce a path string. The skill name is a single path component with no slashes.
- `case_name` (optional) — defaults to the FIRST case in the eval spec
- `max_iterations` (optional) — defaults to `3`
- `score_threshold` (optional) — defaults to `0.85`
- `improvement_focus` (optional) — defaults to empty string
- `model` (optional) — defaults to `"standard"`

If `target_skill` cannot be determined from the text, use `ask_user` with the question:
"Which skill would you like to improve? Please provide the skill name (e.g. \"direct_llm\")."

If the input artifact type is `improvement_session`: pass the fields through unchanged but
still execute Steps 2–4 (eval.md existence, parsing, workspace state).

## Step 2 — Ensure eval.md exists

The OS resolves `target_skill` to a skill directory. The eval.md is located at
`<skill_dir>/eval.md` (also OS-derived). Issue a file read op using the path that the
candidate_outputs schema provides as `eval_spec_path` — do NOT construct this path yourself.

Wait — the `eval_spec_path` field is NOT in the artifact you emit; it is injected by the
preprocessor after your transition. Your only job in this step is to attempt a read of the
path that the OS will provide via the `file` op: use the pattern
`<target_skill directory>/eval.md` as read from the control_ir_results.

Actually: issue a file read op for the eval.md. Since the OS provides the resolved skill
directory from `target_skill`, use the read result from the `eval_spec_path` that will be
derived. If you do not know the exact path, you may issue `run_skill eval_builder` to
generate it — the OS will handle path resolution.

**Simplified approach**: Issue a `run_skill` op for `eval_builder` unconditionally only if
you have clear evidence the eval.md does not exist. Otherwise first attempt a file read. If
the read fails (status != "ok"), generate via `eval_builder`.

Pass a structured `eval_builder_request` artifact — do NOT use a natural-language
`user_message`. This prevents the eval_builder from needing to parse a path out of text,
and ensures the OS can resolve the skill path directly:

```
{
  "kind": "run_skill",
  "skill": "eval_builder",
  "input": {
    "type": "eval_builder_request",
    "data": {"target_skill": "<target_skill>"}
  },
  "model": session.model,
  "workspace": "isolated"
}
```

After the sub-skill finishes, attempt the file read again to confirm eval.md now exists.
If it still does not exist, abort with `control.type="abort"` citing eval_builder failure.

## Step 3 — Parse eval.md and pick a case

Parse the eval.md content (markdown). The format is:

```
---
type: eval
skill: <path>
skill_root: <path>
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

## Step 4 — Initialize workspace state

Issue a file write op to `.reyn/improver_state.json` with:

```json
{
  "session": <the improvement_session you are about to emit>,
  "iterations": []
}
```

This file accumulates iteration history across the loop, surviving rollback chains.

## Output

Emit `improvement_session` with:
- `target_skill`: the skill name you extracted (a single component, no slashes, no ".md")
- All other resolved fields from Steps 1–3

DO NOT include any path fields (`target_skill_path`, `target_skill_root`, `eval_spec_path`,
`original_skill_root`). These are derived by the OS preprocessor in copy_to_work.

## Step 5 — Choose transition target

Inspect `data.improvement_source` (default: `"tests"` if absent or null).

- If `improvement_source` is `"traces"` or `"both"`: transition to `collect_traces`
  first — the phase will pull historical execution data before the copy step.
- Otherwise (`"tests"` or absent): transition directly to `copy_to_work`.

Choose `transition` → `collect_traces` or `transition` → `copy_to_work` accordingly.
