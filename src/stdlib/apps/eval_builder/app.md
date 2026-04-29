---
type: app
name: eval_builder
description: Auto-generate an eval spec (eval.md) for an app
entry: analyze_app
final_output: eval_spec_result
final_output_description: |
  Path to the generated eval.md plus case/criterion counts and a brief summary.
  The user runs the spec separately with `reyn eval <eval_md_path>`.
finish_criteria:
  - eval.md has been written next to the target app's app.md
  - eval_spec_result captures the path, case count, and criterion count
graph:
  analyze_app: [write_eval]
---

## Overview

Reads a target app's DSL files and generates a per-phase quality-criteria
eval.md spec. Does not run the spec — invoke `reyn eval` separately.

## Input

```
reyn run eval_builder "Generate an eval.md for reyn/local/my_app/app.md"
```

## Output

Path to the generated eval.md (alongside the app's DSL files). Run it with:

```
reyn eval reyn/local/my_app/eval.md --model standard
```
