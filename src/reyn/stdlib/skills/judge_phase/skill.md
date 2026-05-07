---
type: skill
name: judge_phase
description: Evaluate a single phase artifact against quality criteria and return a structured judgment.
entry: judge
final_output: phase_judgment_raw
final_output_description: |
  LLM judgment for one phase: per-criterion pass/fail with reasons, an
  overall `passed` boolean, and a one-sentence summary. The numeric `score`
  is added deterministically by the postprocessor.
finish_criteria:
  - Every criterion in the input has been evaluated with a reason
  - passed reflects whether all required criteria are met
graph: {}
permissions:
  python:
    - module: ./postprocessor.py
      function: compute_score
      mode: pure
      timeout: 5
postprocessor:
  output_name: phase_judgment
  output_description: |
    Caller-facing judgment with deterministic `score` (= passed/total)
    merged in by the postprocessor.
  output_schema:
    type: object
    properties:
      phase_name:
        type: string
      passed:
        type: boolean
      score:
        type: number
        minimum: 0.0
        maximum: 1.0
      criteria_results:
        type: array
        items:
          type: object
          properties:
            description: {type: string}
            required: {type: boolean}
            met: {type: boolean}
            reason: {type: string}
          required: [description, met, reason]
      summary:
        type: string
    required: [phase_name, passed, score, criteria_results, summary]
  steps:
    - type: python
      module: ./postprocessor.py
      function: compute_score
      into: data.score
      output_schema:
        type: number
        minimum: 0.0
        maximum: 1.0
---

## Overview

`judge_phase` is a stdlib sub-skill used by the `eval` skill's preprocessor.
It receives one `phase_eval_request` (a phase artifact + criteria list) and
returns a `phase_judgment` (caller-facing artifact with deterministic `score`).

Internally the LLM produces a `phase_judgment_raw` (no `score` field) and
the skill's postprocessor computes `score = passed/total` of
`criteria_results` in pure Python before handing the result to the caller.
This isolates the LLM from arithmetic it is unreliable at.

It is not designed to be run standalone — invoke it via
`iterate × run_skill(judge_phase)` in an eval skill preprocessor.
