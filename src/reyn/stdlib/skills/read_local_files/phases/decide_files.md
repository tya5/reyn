---
type: phase
name: decide_files
input: user_message
role: file_planner
can_finish: false
allowed_ops: []
max_act_turns: 1
---

Decide which local files need to be read to answer the user's request.

## Input

`input_artifact.data.text` — the user's prompt, verbatim.

## Task

Pick the **smallest useful set of project-relative paths** that, once
read, would let a follow-up LLM call answer the user's question. Then
transition to the `read_and_respond` phase with that plan.

Be concrete:

- The user named a path → use it directly (`src/foo.py`, `docs/README.md`).
- The user named a feature or concept → pick the most likely 1–3 paths
  (e.g. "philosophy section of the README" → `["README.md"]`).
- The user named a directory → list the directory's main entry file
  (`__init__.py`, `index.md`, `mod.rs`, …) rather than every file.

## Constraints

- Project-relative paths only. **Never** prefix with `/`, `~`, or
  `../`. The filesystem MCP server enforces its own root and will
  return `status: error` on absolute or escaping paths.
- 1–5 paths is the sweet spot. The downstream phase has `max_act_turns: 3`
  so a plan with 10 reads will not finish.
- Do not invent paths whose existence you cannot reasonably infer from
  the prompt or general project knowledge — the MCP read will simply
  fail and the final answer will be poorer.
- One act turn, then transition. No clarifying questions.

## Tone of `reason`

One short sentence in the user's language explaining *why* these paths.
The `read_and_respond` phase uses it as a focus hint — keep it about
intent, not mechanics ("user is asking about the philosophy section",
not "we will call read_text_file on these paths").
