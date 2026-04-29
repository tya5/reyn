---
type: skill
name: eval
description: Evaluate a target skill against a single test case using judge_phase as LLM-as-judge.
entry: run_target
final_output: eval_result
final_output_description: |
  Evaluation result for one test case: overall pass/fail verdict, score, per-criterion summary, and weakest phase.
finish_criteria:
  - All phases with criteria have been evaluated by judge_phase via the preprocessor
  - overall_score reflects the fraction of all criteria met across evaluated phases
  - weakest_phase identifies the lowest-scoring phase
graph:
  run_target: [evaluate]
---

## Overview

`eval` is a stdlib skill that evaluates one test case of a target skill using `judge_phase` as an LLM judge.

Each invocation handles **one eval case**. The caller (e.g. `reyn eval` CLI or another skill) is responsible for iterating over multiple cases and aggregating the per-case results.

## Execution flow

1. `run_target` — runs the target skill with the test input; builds a list of `phase_eval_request` items from the phase artifacts and quality criteria
2. `evaluate` — preprocessor iterates `judge_phase` over each eval request; LLM aggregates judgments into `eval_result`

## Input

Pass an `eval_case_input` artifact:

```json
{
  "type": "eval_case_input",
  "data": {
    "case_name": "my_case",
    "case_input": "Build an article skill",
    "spec_path": "reyn/local/my_app/eval.md",
    "target_skill_path": "reyn/local/my_app/skill.md",
    "phase_criteria": [
      {
        "phase_name": "generate",
        "criteria": [
          {"description": "Output contains a title", "required": true}
        ]
      }
    ]
  }
}
```
