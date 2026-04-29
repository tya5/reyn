---
type: skill
name: judge_phase
description: Evaluate a single phase artifact against quality criteria and return a structured judgment.
entry: judge
final_output: phase_judgment
final_output_description: |
  Structured judgment for one phase: per-criterion pass/fail with reasons, an overall score (0–1), and a one-sentence summary.
finish_criteria:
  - Every criterion in the input has been evaluated with a reason
  - passed reflects whether all required criteria are met
  - score is the fraction of all criteria met
graph: {}
---

## Overview

`judge_phase` is a stdlib sub-app used by the `eval` app's preprocessor.
It receives one `phase_eval_request` (a phase artifact + criteria list) and returns a `phase_judgment`.

It is not designed to be run standalone — invoke it via `iterate × run_skill(judge_phase)` in an eval app preprocessor.
