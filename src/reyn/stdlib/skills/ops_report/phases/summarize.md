---
type: phase
name: summarize
input: ops_report_collect_output
role: report_generator
can_finish: true
allowed_ops: []
---

Generate a human-readable operational summary report from the aggregated
execution statistics. Your output is the final artifact for this skill.

## Inputs

You receive `data.aggregate` — a stats dict with these keys:

- `total_runs` — total skill executions observed in the period
- `success_count` / `failure_count` — raw counts
- `success_rate` — float 0.0–1.0, or null if no runs observed
- `period_days` — number of days covered (may be null for recall-based path)
- `by_skill` — per-skill breakdown:
  `{skill_name: {count, success, failure, avg_duration_seconds}}`
- `top_failing_skills` — list of `{skill, failure_count, total_count}`,
  sorted by failure_count descending
- `errors_sample` — up to 5 recent error message excerpts

You also receive `data.period` (from the original input) and
`data.focus` (null or a focus string like `"failures"`).

## Decision: Produce `ops_report_output`

Generate a concise, actionable weekly operations report. Rules:

1. **Narrative first**: open with a one-paragraph summary covering total
   runs, overall success rate, and the period.

2. **Top failing skills table**: if `top_failing_skills` is non-empty,
   include a section listing each skill, its failure count, and failure rate
   (failure_count / total_count).

3. **Recommendations** (required even if empty list):
   - If `success_rate < 0.8` → recommend reviewing the skills in
     `top_failing_skills` for recurring errors.
   - If any error in `errors_sample` mentions "timeout" or "TimeoutError"
     → recommend checking `safety.timeout.phase_seconds` configuration
     (FP-0004).
   - If `total_runs == 0` → recommend running `reyn run index_events` to
     populate the events index, then retry.
   - Otherwise: include at least one general health observation.

4. **Period string**: derive from `data.period` or `period_days` — e.g.
   `"last 7 days"` or `"2026-W19"`.

5. Do NOT include raw JSON blobs or internal field names in
   `summary_markdown`. Use human-friendly phrasing.

## Output

Produce an `ops_report_output` artifact with:

- `period` — human-readable period string (e.g. `"last 7 days"`)
- `total_runs` — integer from `data.aggregate.total_runs`
- `success_rate` — float or null from `data.aggregate.success_rate`
- `failure_breakdown` — list from `top_failing_skills` (pass through as-is)
- `cost_total_usd` — null (Phase 1: cost data not aggregated from raw events)
- `top_failing_skills` — list of skill names only (strings), ordered by
  failure count descending
- `recommendations` — list of strings (your generated recommendations)
- `summary_markdown` — Markdown-formatted narrative report

Emit `decision: "finish"` with the `ops_report_output` artifact.
