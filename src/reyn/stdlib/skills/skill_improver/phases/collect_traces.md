---
type: phase
name: collect_traces
input: improvement_session
role: trace_collector
model_class: standard
can_finish: false
max_act_turns: 0
allowed_ops: []
preprocessor:
  # Step 1: query the events recall index for runs of the target skill.
  # Uses a generic query; the python step below filters by skill_name.
  # on_error: skip — if the recall index is not populated (FP-0009 not run),
  # the python step falls back to raw event files automatically.
  - type: run_op
    op:
      kind: recall
      query: "skill execution failures errors phases"
      sources: ["events"]
      top_k: 40
    into: data.trace_recall_result
    on_error: skip

  # Step 2: aggregate trace data (recall or raw-events fallback).
  # Writes summary dict to data.traces_summary.
  - type: python
    module: ./trace_collector.py
    function: collect_traces
    into: data.traces_summary
    mode: unsafe
    output_schema:
      type: object
      required: [skill_name, runs_analyzed, data_source, summary_markdown]
      properties:
        skill_name:       {type: string}
        runs_analyzed:    {type: integer, minimum: 0}
        data_source:      {type: string, enum: [recall, raw_events, empty]}
        summary_markdown: {type: string}
        success_rate:     {type: [number, "null"]}
        top_errors:
          type: array
          items:
            type: object
            properties:
              phase: {type: string}
              msg:   {type: string}
              count: {type: integer}
---

# collect_traces

The preprocessor has queried the events index (or fallen back to raw event files)
and written a trace summary to `data.traces_summary`.

Inspect the summary at `data.traces_summary` and confirm basic sanity:
- `runs_analyzed >= 0`
- `data_source` is one of: `recall`, `raw_events`, `empty`
- `summary_markdown` is a non-empty string

Then emit a `skill_improver_traces` artifact carrying:
- the `summary_markdown` text
- the `runs_analyzed` count
- the `data_source` value
- the original `improvement_session` (passed through verbatim — downstream phases
  use the resolved paths and session state it contains)

If `data_source == "empty"` (no historical data found), still transition forward
with the empty summary — `plan_improvements` will fall back to test-score-only
improvement signal. Do NOT abort.

Choose `transition` → `copy_to_work`.
