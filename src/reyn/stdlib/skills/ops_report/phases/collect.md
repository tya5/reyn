---
type: phase
name: collect
input: ops_report_input
role: data_collector
can_finish: false
allowed_ops: []
preprocessor:
  # Step 1: recall (unchanged). on_error: skip so a missing events index does
  # not abort the phase — dispatch_aggregate handles the None/empty case.
  - type: run_op
    op:
      kind: recall
      query: "skill execution run summary failures errors"
      sources: ["events"]
      top_k: 50
    into: data.recall_result
    on_error: skip

  # Step 2 (R-PURE-MODE wave 3a): pure dispatcher — mode: safe.
  # If recall produced ≥1 chunk, aggregates inline via aggregate_from_recall_chunks
  # and returns {_path: "recall", ...full stats}.
  # Otherwise returns {_path: "needs_fallback", period_days, skills} sentinel.
  # `when:` is NOT supported in preprocessor steps; this step always runs.
  - type: python
    module: ./aggregate_pure.py
    function: dispatch_aggregate
    into: data.aggregate
    mode: safe
    output_schema:
      type: object
      required: [_path]
      properties:
        _path:
          type: string
          enum: [recall, needs_fallback]
        # Full stats fields — present when _path=recall, absent when _path=needs_fallback.
        total_runs:
          type: integer
          minimum: 0
        success_count:
          type: integer
          minimum: 0
        failure_count:
          type: integer
          minimum: 0
        success_rate:
          type: [number, "null"]
        period_days:
          type: [integer, "null"]
        by_skill:
          type: object
          additionalProperties:
            type: object
        top_failing_skills:
          type: array
          items:
            type: object
        errors_sample:
          type: array
          items:
            type: string
        # Needs-fallback carry-through fields (present when _path=needs_fallback).
        skills:
          type: ["array", "null"]
          items:
            type: string

  # Step 3 (R-PURE-MODE wave 3a): fallback — mode: unsafe.
  # No-ops (strips _path sentinel, returns unchanged stats) if dispatch_aggregate
  # set _path=recall. Walks .reyn/events/*.jsonl if _path=needs_fallback.
  # Always runs unconditionally — when: is not supported; internal detection
  # via aggregate._path.
  - type: python
    module: ./aggregate.py
    function: collect_aggregate_fallback
    into: data.aggregate
    mode: unsafe
    output_schema:
      type: object
      required: [total_runs, success_count, failure_count, by_skill,
                 top_failing_skills, errors_sample]
      properties:
        total_runs:
          type: integer
          minimum: 0
        success_count:
          type: integer
          minimum: 0
        failure_count:
          type: integer
          minimum: 0
        success_rate:
          type: [number, "null"]
          minimum: 0.0
          maximum: 1.0
        period_days:
          type: [integer, "null"]
        data_source:
          type: string
          enum: [recall, raw_events, empty]
        by_skill:
          type: object
          description: Per-skill stats keyed by skill name.
          additionalProperties:
            type: object
            required: [count, success, failure]
            properties:
              count:
                type: integer
                minimum: 0
              success:
                type: integer
                minimum: 0
              failure:
                type: integer
                minimum: 0
              avg_duration_seconds:
                type: [number, "null"]
        top_failing_skills:
          type: array
          items:
            type: object
            required: [skill, failure_count, total_count]
            properties:
              skill:
                type: string
              failure_count:
                type: integer
                minimum: 0
              total_count:
                type: integer
                minimum: 0
        errors_sample:
          type: array
          items:
            type: string
---

Inspect the preprocessor-resolved aggregate and transition to the `summarize`
phase with an `ops_report_collect_output` artifact.

The OS preprocessor has already done all the work:

1. It attempted a semantic recall from the events index
   (`recall(sources=["events"], top_k=50)`).
2. It ran `dispatch_aggregate` (mode: safe), which aggregated recall chunks
   inline if present, or emitted a `needs_fallback` sentinel if chunks were empty.
3. It ran `collect_aggregate_fallback` (mode: unsafe), which no-ops if upstream
   already recalled stats, or walks `.reyn/events/*.jsonl` directly as fallback.

Your only job is to confirm that `data.aggregate` looks sane and pass it
through as `ops_report_collect_output`.

## Inputs

- `data.aggregate` — aggregated stats dict produced by `collect_aggregate`.
  Key fields: `total_runs`, `success_count`, `failure_count`, `success_rate`,
  `by_skill`, `top_failing_skills`, `errors_sample`, `data_source`.
- `data.period` — period string from the original input (e.g. `"last-week"`).
- `data.period_days` — effective period in days (integer or null).
- `data.focus` — optional skill-focus filter (string or null).

## Sanity checks

Before transitioning, verify:

1. `data.aggregate.total_runs` is an integer >= 0.
2. If `total_runs > 0`, `success_count + failure_count <= total_runs`
   (in-flight runs may not be counted).
3. `data_source` is one of `recall`, `raw_events`, or `empty`.

If any check fails, abort with a clear reason.

## Decision: Transition to `summarize`

Emit a transition to `summarize` with an `ops_report_collect_output` artifact
that carries `data.aggregate`, `data.period`, `data.period_days`,
`data.focus`, and `data.data_source` (from `data.aggregate.data_source`).

Do NOT compute, rewrite, or modify any aggregate values — pass them through
verbatim from `data.aggregate`.
