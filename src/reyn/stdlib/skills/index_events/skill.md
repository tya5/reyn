---
type: skill
name: index_events
description: |
  P6 イベントログを run 単位で RAG インデックス化する operational intelligence の基盤
  (FP-0009 Component A).

  Phase 1 (LLM): resolve cursor + discover event files, narrate indexing range.
  Phase 2 (Skill.postprocessor): deterministic chunk → provider-direct
  embed+index → cursor-advance pipeline; LLM is not involved.

  Incremental via .reyn/index/events_cursor — only new runs (since last index)
  are processed on each invocation. Failed runs carry truncated error summaries.
entry: scan
final_output: scan_plan
final_output_description: |
  LLM-contract artifact: echoes back the resolved since timestamp, inventory
  summary (count + ts range), optional skill filter, and mode. The skill
  postprocessor re-globs event files deterministically and uses `since` to
  run the deterministic chunk → provider-direct embed+index pipeline.
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
    # FP-0042 Phase 2.3 (2026-05-23): migrated from mode: unsafe to mode: safe.
    # File reads / writes / stat / glob go through reyn.safe.file; the
    # atomic cursor update uses reyn.safe.file.write_atomic. Event-file
    # reads under .reyn/events/ + cursor reads/writes under
    # .reyn/index/events_cursor are inside the default zones. #1303 Stage I:
    # run_collect_chunks now streams chunks into reyn.safe.embed_index (which
    # writes under .reyn/index/, default zone) — no out-of-zone JSONL output,
    # so the old file.write:artifacts grant is dropped.
    - module: ./chunkers.py
      function: resolve_scan_context
      mode: safe
      timeout: 30
    - module: ./chunkers.py
      function: run_collect_chunks
      mode: safe
      timeout: 300
    - module: ./chunkers.py
      function: run_advance_cursor
      mode: safe
      timeout: 10
postprocessor:
  output_schema: index_events_summary
  steps:
    # Step 1: safe — walk events_root, group by run, and stream the chunks
    # into reyn.safe.embed_index (provider-direct embed+index to the "events"
    # source; folds the old embed + index_write run-ops, no intermediate
    # file). #1303 Stage I.
    - type: python
      module: ./chunkers.py
      function: run_collect_chunks
      into: data.chunk_stats
      mode: safe
      output_schema:
        type: object
        required: [chunk_count, skipped_runs, filtered_runs]
        properties:
          chunk_count:      {type: integer, minimum: 0}
          skipped_runs:     {type: integer, minimum: 0}
          filtered_runs:    {type: integer, minimum: 0}
          embedded:         {type: integer, minimum: 0}
          skipped_embed:    {type: integer, minimum: 0}
          written:          {type: integer, minimum: 0}
          skipped_write:    {type: integer, minimum: 0}
          max_completed_at: {type: string}
    # Step 2: advance cursor to max completed_at of this batch
    - type: python
      module: ./chunkers.py
      function: run_advance_cursor
      into: data.cursor_result
      mode: safe
      output_schema:
        type: object
        required: [indexed_runs, new_cursor, sources_updated]
        properties:
          indexed_runs:    {type: integer, minimum: 0}
          skipped_runs:    {type: integer, minimum: 0}
          filtered_runs:   {type: integer, minimum: 0}
          new_cursor:      {type: string}
          sources_updated: {type: array, items: {type: string}}
# FP-0016 D: this skill needs no static secrets / OAuth tokens.
required_credentials: []
---

## Overview

`index_events` indexes the P6 event log at run granularity (1 run = 1 chunk)
into the existing RAG infrastructure (ADR-0033). The chunker streams runs into
`reyn.safe.embed_index` (provider-direct embed+index; #1303 Stage I folded the
old `embed` / `index_write` run-ops) and `recall` reads it back. Incremental
via `.reyn/index/events_cursor`.

## Execution flow

1. **Phase `scan`** (LLM):
   - OS preprocessor runs `resolve_scan_context` to read cursor + summarise file
     inventory (count + ts range; full path list is NOT exposed to the LLM)
   - LLM echoes the resolved `since`, `event_files_count`, `skill_filter`, `mode`
     into the `scan_plan` artifact and finishes immediately

2. **Skill.postprocessor** (deterministic, LLM not involved):
   - `run_collect_chunks` (python step, safe): walks `.reyn/events/` from the
     cursor, groups events by run boundary, and **streams** the chunks into
     `reyn.safe.embed_index.embed_and_index` — which embeds them provider-direct
     and writes the vectors to the `events` index source
     (`.reyn/index/events/index.db`), tracking the max `completed_at`. No
     intermediate file (#1303 Stage I folds the old `embed` + `index_write`
     run-ops into this step). Resume = DB-as-checkpoint.
   - `run_advance_cursor` (python step, safe): writes the max `completed_at`
     (from `data.chunk_stats`) to `.reyn/index/events_cursor`

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
