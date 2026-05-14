---
type: phase
name: scan
input: index_events_input
role: index_planner
can_finish: true
allowed_ops: []
preprocessor:
  - type: python
    module: ./event_chunker.py
    function: resolve_scan_context
    into: data.scan_context
    output_schema:
      type: object
      required: [since, event_files, cursor_exists]
      properties:
        since:
          type: string
          description: Effective lower-bound ISO-8601 timestamp (from input or cursor file).
        event_files:
          type: array
          items:
            type: string
          description: All .jsonl event files found under .reyn/events/.
        cursor_exists:
          type: boolean
          description: Whether .reyn/index/events_cursor was found on disk.
        cursor_value:
          type: [string, "null"]
          description: Raw cursor file contents (null if absent).
---

Produce a `scan_plan` artifact for the index_events postprocessor.

The OS preprocessor has already resolved the effective lower-bound timestamp
and discovered all available event files — use that data directly. Do not
recompute timestamps or file lists yourself.

## Inputs

- **Input since**: `data.since` — caller-supplied ISO timestamp, or null.
- **Input skills**: `data.skills` — skill filter list, or null (= all skills).
- **Input mode**: `data.mode` — `append` or `replace` (default: append).
- **Resolved context**: `data.scan_context`
  - `since` — effective lower-bound timestamp (already resolved from cursor
    or input; use this verbatim as the `since` field in your artifact)
  - `event_files` — list of all discovered event .jsonl paths
  - `cursor_exists` — whether a cursor file was found

## Decision: Produce `scan_plan`

Your only job is to echo the preprocessor-resolved data into a `scan_plan`
artifact so the postprocessor can run the deterministic chunking pipeline.

Rules:
1. Set `since` = `data.scan_context.since` verbatim.
2. Set `event_files` = `data.scan_context.event_files` verbatim (all files;
   the postprocessor filters by timestamp at read time).
3. Set `skill_filter` = `data.skills` (null or the list).
4. Set `mode` = `data.mode` (or `"append"` if absent).
5. Emit `decision: "finish"` with the `scan_plan` artifact.

## Constraints

- Do NOT emit any ops. Emit only a decide turn.
- Do NOT recompute or modify the `since` timestamp.
- Do NOT filter `event_files` — pass the full list; the postprocessor handles
  timestamp filtering efficiently at stream time.
- Fill all required fields of `scan_plan`.
