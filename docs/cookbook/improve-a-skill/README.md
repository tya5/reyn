# improve-a-skill

> ℹ️ Uses a custom skill bundled in this example dir
> (`./sample_skill/skill.md`) — a deliberately weak target so the improver
> has something to fix. Copy it into `reyn/local/sample_skill/` before
> running (see Setup). The improver itself is stdlib.

Iteratively improve a skill: run it, score it, plan changes, apply them,
re-score. Loops until a threshold is met or max iterations hit. Driven by
the `skill_improver` stdlib skill.

## What this shows

- The improver loop: prepare → copy_to_work → run_and_eval → plan →
  apply → finalize.
- Why the improver works on a **temp copy** (changes only land on the
  original on success — safe by default).
- Why this needs `--allow-shell` (the improver runs sub-`reyn` invocations
  to score the work copy).

## Setup — a deliberately weak target skill

This example ships with a tiny `sample_skill/` that's intentionally
under-specified so the improver has something to fix. Copy it into your
local skill area:

```bash
cp -r cookbook/improve-a-skill/sample_skill reyn/local/sample_skill
```

Quick sanity check:

```bash
reyn run sample_skill "summarize: the cat sat on the mat"
```

Output will be vague — that's the point.

## Run the improver

```bash
reyn run skill_improver "improve sample_skill to score >= 0.9 on summarization fidelity" \
    --allow-shell
```

What the improver does:

1. Copies `reyn/local/sample_skill/` → a temp dir.
2. Runs `eval` on the temp copy with auto-generated criteria.
3. Asks the LLM to plan DSL edits (phase instructions, schema tweaks).
4. Applies edits via `write_file` ops.
5. Re-evals. If score ≥ threshold, copies temp → original. Else loops.

## Expected output

```json
{
  "score_history": [0.42, 0.71, 0.93],
  "iterations": 3,
  "termination_reason": "score_threshold_met",
  "files_modified": [
    "reyn/local/sample_skill/phases/summarize.md",
    "reyn/local/sample_skill/skill.md"
  ],
  "next_step": "reyn run sample_skill \"summarize: ...\" to verify"
}
```

## Why `--allow-shell`?

`skill_improver` invokes `reyn run` inside its loop to score the work copy.
The `shell` Control IR op is gated for safety; you opt in per invocation.

## Variations

- Set `--limits.max-iterations 5` to cap the loop.
- Improve any other skill — replace `sample_skill` with the target name.

## See also

- [stdlib/skill_improver](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/skill_improver/skill.md)
- [eval-a-skill](../eval-a-skill/README.md) — the scoring step in isolation.
