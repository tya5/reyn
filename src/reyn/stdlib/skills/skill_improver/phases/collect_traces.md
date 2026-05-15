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
  # dispatch_traces handles the None/empty case and emits the fallback sentinel.
  - type: run_op
    op:
      kind: recall
      query: "skill execution failures errors phases"
      sources: ["events"]
      top_k: 40
    into: data.trace_recall_result
    on_error: skip

  # Step 2 (R-PURE-MODE wave 4): pure dispatcher — mode: safe.
  # If recall produced ≥1 chunk, aggregates inline and returns
  # {_path: "recall", ...full summary}.
  # Otherwise returns {_path: "needs_fallback", target_skill, trace_lookback_runs}
  # sentinel.
  # `when:` is NOT supported in preprocessor steps; this step always runs.
  - type: python
    module: ./trace_collector_pure.py
    function: dispatch_traces
    into: data.traces_summary
    mode: safe
    output_schema:
      type: object
      required: [_path]
      properties:
        _path:
          type: string
          enum: [recall, needs_fallback]
        # Full summary fields — present when _path=recall, absent when _path=needs_fallback.
        skill_name:
          type: string
        runs_analyzed:
          type: integer
          minimum: 0
        data_source:
          type: string
        summary_markdown:
          type: string
        success_rate:
          type: [number, "null"]
        top_errors:
          type: array
          items:
            type: object
            properties:
              phase: {type: string}
              msg:   {type: string}
              count: {type: integer}
        # Needs-fallback carry-through fields (present when _path=needs_fallback).
        target_skill:
          type: string
        trace_lookback_runs:
          type: integer

  # Step 3 (R-PURE-MODE wave 4): fallback — mode: unsafe.
  # No-ops (strips _path sentinel, returns unchanged summary) if dispatch_traces
  # set _path=recall. Walks .reyn/events/**/*.jsonl if _path=needs_fallback.
  # Always runs unconditionally — when: is not supported; internal detection
  # via traces_summary._path.
  - type: python
    module: ./trace_collector.py
    function: collect_traces_fallback
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

The OS preprocessor has already done all the work:

1. It attempted a semantic recall from the events index
   (`recall(sources=["events"], top_k=40)`).
2. It ran `dispatch_traces` (mode: safe), which aggregated recall chunks
   inline if present, or emitted a `needs_fallback` sentinel if chunks were empty.
3. It ran `collect_traces_fallback` (mode: unsafe), which no-ops if upstream
   already recalled stats, or walks `.reyn/events/**/*.jsonl` directly as fallback.

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
