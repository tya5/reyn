---
scenario: B4-S3
title: skill_improver nested run_skill chain observation
date: 2026-05-04
batch: 4
status: completed
---

# B4-S3 — skill_improver nested run_skill chain

## Setup

- **Entry skill**: `skill_improver`
- **Target**: `src/reyn/stdlib/skills/direct_llm/skill.md`
- **Input**: `improvement_session` JSON with `max_iterations=1, score_threshold=0.99`
- **Model**: `openai/gemini-2.5-flash-lite` via LiteLLM proxy at localhost:4000
- **Run ID**: `20260504T024717Z_skill_improver`

## Observed Chain Depth

3 layers (not 4 as hypothesized — see Note below):

```
Layer 1: skill_improver        run_id=20260504T024717Z_skill_improver
  └─ Layer 2: eval_builder     run_id=20260504T024719Z_eval_builder   (from prepare phase)
  └─ Layer 2: eval             run_id=20260504T024747Z_eval           (from run_and_eval phase)
       └─ Layer 3: direct_llm  (attempted from run_target but failed — see Bug B4-BUG-1)
```

`judge_phase` was NOT reached as a separate layer — it is invoked via the `iterate` preprocessor
inside `eval.evaluate`, not via `run_skill`. It is therefore not a 4th `run_skill` layer.

## Phase Sequence

```
[START skill_improver]
  prepare (visit #1)
    → run_skill eval_builder      [Layer 2 — auto-generates eval.md]
      analyze_skill → write_eval → FINISH
    (eval_builder finished)
  copy_to_work (visit #1)         [BUG: files NOT written to work dir — see B4-BUG-1]
  run_and_eval (visit #1)
    → run_skill eval              [Layer 2]
      run_target → evaluate → FINISH
      (run_target: run_skill direct_llm failed → [Errno 2] no such file .reyn/skill_improver_work/direct_llm/skill.md)
    (eval finished)
  plan_improvements (visit #1)    [changes=[] because eval scored 0.0]
  apply_improvements (visit #1)   [no files written]
  finalize (visit #1)
[FINISH]
```

## `run_skill` Control IR Appearances

| Phase | Op | Skill | Status |
|-------|----|-------|--------|
| `prepare` | `run_skill` | `eval_builder` | finished |
| `run_and_eval` | `run_skill` | `eval` | finished |
| `eval.run_target` | `run_skill` | `direct_llm` (via `target_skill_path`) | error (file not found) |

The `run_skill` op in `run_and_eval` (step 3 per `run_and_eval.md`) and in `eval.run_target` are both
visible in the event log as `tool_executed`/`tool_returned` pairs with `tool="run_skill"`.

## skill_runs/ Directory Structure

No `skill_runs/` directory under `.reyn/state/`. The OS stores per-run events in `.reyn/events/direct/skill_runs/` 
(flat files by skill name), not in a nested state tree. There is no `parent_run_id` field in
`run_skill_started` / `run_skill_completed` events:

```json
{"type": "run_skill_started", "data": {"skill": "eval_builder", "state_dir": "..."}}
{"type": "run_skill_completed", "data": {"skill": "eval_builder", "status": "finished", ...}}
```

**Finding**: Parent→child linkage is absent from event data. The nesting is only reconstructable
by scanning the `skill_improver` event file for `run_skill_started` events (which embed child
`workflow_started` events inline). This confirms the need for `parent_run_id` in the tree display
motivation mentioned in R-D13.

## Token Cost per Layer

| Layer | Skill | Prompt tok | Completion tok | Total |
|-------|-------|-----------|----------------|-------|
| L1 | skill_improver (own phases) | 146,134 | 7,965 | 154,099 |
| L2a | eval_builder | 23,091 | 1,255 | 24,346 |
| L2b | eval | 13,101 | 527 | 13,628 |
| L3 | direct_llm | 0 (aborted) | 0 | 0 |
| **Total** | | **182,326** | **9,747** | **192,073** |

Reported cost: ~$0.0178 (gemini-2.5-flash-lite via LiteLLM proxy).

## Final Output (improvement_result)

- `termination_reason`: `no_more_changes_planned`
- `iterations_performed`: 1
- `initial_score`: 0.0
- `final_score`: 0.0
- `files_modified`: `[]`
- `copied_back`: `false`

The improvement cycle completed structurally but produced no actual improvement because the
eval scored 0.0 (target skill not found in work dir → `run_skill` error in `run_target`).

## Bugs Found

### B4-BUG-1 [HIGH] — copy_to_work reads files but does not write them

**Root cause**: `copy_to_work` has `max_act_turns: 3`. The LLM consumed:
- Turn 1: 3 glob ops (correct)
- Turn 2: 5 file reads + 4 reads (the direct_llm files, but also wrongly re-reads non-target files)
- Turn 3: hit budget on MORE reads instead of writes

Result: `.reyn/skill_improver_work/direct_llm/` was never created. Subsequent `eval.run_target`
attempted `run_skill` with `skill=".reyn/skill_improver_work/direct_llm/skill.md"` → `[Errno 2]`.

The act turn budget (3) is too tight for the copy-all-files workflow when the LLM uses redundant
reads. The phase instructions say "All writes go in this single act turn" but the LLM consumed
turn 3 on duplicate reads.

**Evidence**: `plan_improvements` glob for `.reyn/skill_improver_work/direct_llm/**/*.md` returned
0 matches; `eval.run_target` `tool_returned` shows `status=error`.

**Suggested fix**: Raise `max_act_turns` to 4 in `copy_to_work.md`, or restructure the phase to
read-and-write in the same turn (combined ops).

### B4-BUG-2 [MED] — eval.md path mismatch between eval_builder output and prepare expectation

`eval_builder.write_eval` wrote eval.md to `reyn/local/direct_llm/eval.md`. The `prepare` phase
then looked for it at `src/reyn/stdlib/skills/direct_llm/eval.md` (4 failed reads before finding
`reyn/local/direct_llm/eval.md` on act turn 6). This is a path convention mismatch: `eval_builder`
always writes to `reyn/local/<skill_slug>/eval.md` but `prepare` defaults to
`<target_dsl_root>/eval.md`. Requires extra act turns and risks act budget exhaustion.

### B4-BUG-3 [LOW] — copy_to_work globs entire stdlib, not just the target skill

Turn 1 issued glob for `src/reyn/stdlib/skills/**/*.md` (all skills, 39 matches) rather than
`src/reyn/stdlib/skills/direct_llm/**/*.md`. The LLM then read files from `word_stats_demo`
alongside `direct_llm`. This wastes act turns and tokens.

## parent_run_id Chain Evidence (for R-D13 tree display)

None of the 3 event files carries a `parent_run_id` field. The nested structure is only visible
by event co-location in `2026-05-04T114717_skill_improver.jsonl` (all events from all 3 runs are
interleaved in the top-level file). This makes a flat log very hard to read — confirming that
R-D13's tree display requirement is well-motivated.

Proposed minimal fix: emit `parent_run_id` in `run_skill_started` and in the child
`workflow_started` event.

## Summary

3-layer nested chain confirmed: `skill_improver → eval_builder`, `skill_improver → eval`,
`eval → direct_llm (attempted)`. The 4th layer (`judge_phase`) runs as a preprocessor `iterate`
loop inside `eval.evaluate`, not as a `run_skill` child. The chain completed structurally but
produced score=0.0 because `copy_to_work` failed to write the work dir. Two HIGH/MED bugs
identified (`copy_to_work` act-budget overflow, eval.md path mismatch). No cost explosion observed
($0.018 total for 3-layer run). Final output reached the user as `improvement_result` JSON.
