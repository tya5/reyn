---
type: phase
name: scan
input: index_chat_input
role: index_planner
can_finish: true
allowed_ops: []
preprocessor:
  - type: python
    module: ./chunkers.py
    function: resolve_chat_scan_context
    into: data.scan_context
    output_schema:
      type: object
      required: [since, chat_files_count, mode, cursor_exists]
      properties:
        since:
          type: string
          description: Effective lower-bound ISO-8601 timestamp (from input or chat cursor file).
        chat_files_count:
          type: integer
          minimum: 0
          description: Number of candidate .jsonl files found under .reyn/events/agents/*/chat/.
        oldest_timestamp:
          type: [string, "null"]
          description: Approximate timestamp of the oldest chat file (by mtime). Null if no files.
        newest_timestamp:
          type: [string, "null"]
          description: Approximate timestamp of the newest chat file (by mtime). Null if no files.
        mode:
          type: string
          description: '"append" or "replace" — from input or defaulted.'
        cursor_exists:
          type: boolean
          description: Whether .reyn/index/chat_cursor was found on disk.
        cursor_value:
          type: [string, "null"]
          description: Raw chat cursor file contents (null if absent).
---

Produce a `chat_scan_plan` artifact for the index_chat postprocessor.

The OS preprocessor has already resolved the effective lower-bound timestamp
and summarised the available chat file inventory — use that data directly.
Do not recompute timestamps or enumerate files yourself.

## Inputs

- **Input since**: `data.since` — caller-supplied ISO timestamp, or null.
- **Input mode**: `data.mode` — `append` or `replace` (default: append).
- **Resolved context**: `data.scan_context`
  - `since` — effective lower-bound timestamp (already resolved from cursor
    or input; use this verbatim as the `since` field in your artifact)
  - `chat_files_count` — number of candidate .jsonl files discovered
  - `oldest_timestamp` / `newest_timestamp` — inventory date range (informational)
  - `mode` — indexing mode (pass through verbatim)
  - `cursor_exists` — whether a chat cursor file was found

## Decision: Produce `chat_scan_plan`

Your only job is to echo the preprocessor-resolved data into a `chat_scan_plan`
artifact so the postprocessor can run the deterministic chunking pipeline.

Rules:
1. Set `since` = `data.scan_context.since` verbatim.
2. Set `chat_files_count` = `data.scan_context.chat_files_count` verbatim.
3. Set `oldest_timestamp` = `data.scan_context.oldest_timestamp` verbatim.
4. Set `newest_timestamp` = `data.scan_context.newest_timestamp` verbatim.
5. Set `mode` = `data.scan_context.mode`.
6. Emit `decision: "finish"` with the `chat_scan_plan` artifact.

## Constraints

- Do NOT emit any ops. Emit only a decide turn.
- Do NOT recompute or modify the `since` timestamp.
- Do NOT include file paths — the postprocessor re-globs files itself.
- Fill all required fields of `chat_scan_plan`.
