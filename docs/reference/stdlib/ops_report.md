---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [ops_report]
---

# `ops_report`

Generate an execution summary from the indexed P6 event log covering a specified period.

## Entry

`collect`

## Final output

`ops_report_output` — `period`, `total_runs`, `success_rate`, `failure_breakdown`, `cost_total_usd` (always `null` in current implementation), `top_failing_skills`, `recommendations`, `summary_markdown`.

## How it composes

Two phases: `collect → summarize`. In `collect`, the `aggregate.py` preprocessor (unsafe mode, 30 s timeout) runs a `recall(sources=["events"])` op; if recall returns nothing, it falls back to direct `.reyn/events/*.jsonl` scanning. The LLM in `collect` only validates sanity and passes data through. The `summarize` phase generates the Markdown narrative and recommendations.

## Caveats

Requires `file.read` on `.reyn/events/` and `.reyn/index/`. The recall path is only useful after `index_events` has been run; without it the skill falls back to raw event scanning. `cost_total_usd` is always `null` — cost aggregation is not yet implemented.

## Usage

```bash
reyn run ops_report '{"period": "last-week"}'
reyn run ops_report '{"period": "last-week", "focus": "failures", "skills": ["my_skill"]}'
```

Optional fields: `period_days`, `focus`, `format`, `skills`.

## Source

[`src/reyn/stdlib/skills/ops_report/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/ops_report/skill.md)
