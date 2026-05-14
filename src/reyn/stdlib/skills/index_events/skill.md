---
type: skill
name: index_events
description: |
  P6 イベントログを run 単位で RAG インデックス化する operational intelligence の基盤
  (FP-0009 Component A).

  Phase 1 (LLM): resolve cursor + discover event files, narrate indexing range.
  Phase 2 (Skill.postprocessor): deterministic chunk → embed → index_write →
  cursor-advance pipeline; LLM is not involved.

  Incremental via .reyn/index/events_cursor — only new runs (since last index)
  are processed on each invocation. Failed runs carry truncated error summaries.
entry: scan
final_output: scan_plan
final_output_description: |
  LLM-contract artifact: echoes back the resolved since timestamp, inventory
  summary (count + ts range), optional skill filter, and mode. The skill
  postprocessor re-globs event files deterministically and uses `since` to
  run the deterministic chunk → embed → index_write pipeline.
finish_criteria:
  - Preprocessor-resolved scan context was reviewed
  - scan_plan artifact echoes since, event_files_count, skill_filter, and mode
search_hints:
  - "index my event log for recall queries"
  - "build operational intelligence from P6 events"
  - "make execution history searchable"
  - "index skill run history"
graph:
  scan: []
permissions:
  python:
    - module: ./event_chunker.py
      function: resolve_scan_context
      mode: unsafe
      timeout: 30
    - module: ./chunkers.py
      function: run_collect_chunks
      mode: unsafe
      timeout: 300
    - module: ./chunkers.py
      function: run_advance_cursor
      mode: unsafe
      timeout: 10
postprocessor:
  output_schema: index_events_summary
  steps:
    # Step 1: unsafe — walk events_root, group by run, produce chunks.jsonl
    - type: python
      module: ./chunkers.py
      function: run_collect_chunks
      into: data.chunk_stats
      mode: unsafe
      output_schema:
        type: object
        required: [chunk_count, skipped_runs, filtered_runs]
        properties:
          chunk_count:    {type: integer, minimum: 0}
          skipped_runs:   {type: integer, minimum: 0}
          filtered_runs:  {type: integer, minimum: 0}
    # Step 2: embed the chunks.jsonl artifact
    - type: run_op
      into: data.embed_result
      op:
        kind: embed
        input_artifact: artifacts/event_chunks.jsonl
        output_artifact: artifacts/event_chunks_with_vectors.jsonl
      args_from: {}
    # Step 3: write to "events" index source
    - type: run_op
      into: data.index_result
      op:
        kind: index_write
        source: events
        input_artifact: artifacts/event_chunks_with_vectors.jsonl
        mode: append
        description: "P6 event runs indexed by index_events skill"
      args_from: {}
    # Step 4: advance cursor to max completed_at of this batch
    - type: python
      module: ./chunkers.py
      function: run_advance_cursor
      into: data.cursor_result
      mode: unsafe
      output_schema:
        type: object
        required: [indexed_runs, new_cursor, sources_updated]
        properties:
          indexed_runs:    {type: integer, minimum: 0}
          skipped_runs:    {type: integer, minimum: 0}
          filtered_runs:   {type: integer, minimum: 0}
          new_cursor:      {type: string}
          sources_updated: {type: array, items: {type: string}}
---

## Overview

`index_events` indexes the P6 event log at run granularity (1 run = 1 chunk)
into the existing RAG infrastructure (ADR-0033). Uses `embed` / `index_write`
/ `recall` ops — zero OS changes. Incremental via `.reyn/index/events_cursor`.

## Execution flow

1. **Phase `scan`** (LLM):
   - OS preprocessor runs `resolve_scan_context` to read cursor + summarise file
     inventory (count + ts range; full path list is NOT exposed to the LLM)
   - LLM echoes the resolved `since`, `event_files_count`, `skill_filter`, `mode`
     into the `scan_plan` artifact and finishes immediately

2. **Skill.postprocessor** (deterministic, LLM not involved):
   - `collect_run_chunks` (python step, unsafe): walks `.reyn/events/` from
     cursor, groups events by run boundary, writes `artifacts/event_chunks.jsonl`
   - `embed` (run_op): embeds each chunk's text field
   - `index_write` (run_op): writes to `events` index source
   - `advance_cursor` (python step, unsafe): writes max completed_at to
     `.reyn/index/events_cursor`

## Input

```
reyn run index_events
reyn run index_events --input '{"since": "2026-05-14T00:00:00"}'
reyn run index_events --input '{"skills": ["my_skill"], "mode": "replace"}'
```

## Output

`index_events_summary` with:
- `indexed_runs` — complete runs indexed in this invocation
- `skipped_runs` — incomplete runs (no completion event) deferred to next pass
- `filtered_runs` — runs excluded by since / skill_filter
- `new_cursor` — ISO timestamp written to `.reyn/index/events_cursor`

## Recall pattern (Component C)

```yaml
- op: recall
  query: "my_skill failure error phase"
  sources: ["events"]
  top_k: 10
```
