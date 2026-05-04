---
type: phase
name: copy_to_work
input: improvement_session
role: workspace_initializer
max_act_turns: 6
allowed_ops: [file]
---

Copy the target skill's DSL files to a temp work directory and emit an updated session pointing at the copy.

You have up to 6 act turns. Use them in order — complete all three stages before the decide turn.

## Compute before acting

From `input_artifact.data`:
- `original_dsl_root` = `target_dsl_root` (e.g. `"src/stdlib/skills/word_stats_demo"` or `"reyn/local/my_app"`)
- `skill_slug` = last path component of `original_dsl_root` (e.g. `"word_stats_demo"`)
- `work_dir` = `.reyn/skill_improver_work/<skill_slug>` (e.g. `.reyn/skill_improver_work/word_stats_demo`)

## Act turn 1 — Glob source files

Issue exactly these three ops using `original_dsl_root` as the prefix — do NOT glob parent directories or sibling skills:

```json
{"kind": "file", "op": "glob", "path": "<original_dsl_root>/**/*.md"}
{"kind": "file", "op": "glob", "path": "<original_dsl_root>/**/*.yaml"}
{"kind": "file", "op": "glob", "path": "<original_dsl_root>/**/*.py"}
```

Combine all three result lists. Remove any entry whose filename is `eval.md`.

IMPORTANT: the glob patterns MUST start with `original_dsl_root` (e.g. `src/reyn/stdlib/skills/word_stats_demo/**/*.md`), not with a parent path like `src/reyn/stdlib/skills/**/*.md`. Globbing a parent directory wastes act turns reading unrelated skills.

## Act turn 2 — Read all source files

For every path in the combined list, issue one `file read` op. All reads go in this single act turn.

## Act turn 3 — Write copies to work dir

For every file read in Act turn 2:
1. `relative_path` = path with `<original_dsl_root>/` prefix stripped
2. Issue: `{"kind": "file", "op": "write", "path": "<work_dir>/<relative_path>", "content": <read_content>}`

All writes go in this single act turn.

## Decide turn — Emit updated session

After Act turn 3 results arrive, emit `improvement_session` with these fields updated:
- `original_dsl_root` = `original_dsl_root` (the value you computed above)
- `target_dsl_root` = `work_dir`
- `target_skill_path` = `<work_dir>/skill.md`
- All other fields copied unchanged from input

Choose `transition` → `run_and_eval`.
