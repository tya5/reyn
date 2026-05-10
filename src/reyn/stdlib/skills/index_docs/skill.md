---
type: skill
name: index_docs
description: |
  Build a searchable semantic index over a path glob (ADR-0033 §2.1).

  Phase 1 (LLM): inspect file samples + cost preflight, decide a chunk
  strategy, or abort if cost is unexpectedly high.
  Phase 2 (Skill.postprocessor): deterministic chunk → embed → index_write
  pipeline; LLM is not involved.

  Override the chunkers module for project-specific formats (Python AST,
  custom Markdown, etc.) via `extends: stdlib/index_docs` + module override.
entry: strategy
final_output: chunk_strategy
final_output_description: |
  LLM-contract artifact: chunking strategy decision (boundary, chunk size,
  overlap, parent context flag) plus echoed passthrough fields (source,
  path, description, mode). The skill postprocessor uses this to run the
  deterministic chunk → embed → index_write pipeline.
finish_criteria:
  - File samples and cost preflight were reviewed
  - A chunk_strategy was decided (or abort was issued for high-cost inputs)
  - source, path, description, and mode are echoed in the artifact
graph:
  strategy: []
permissions:
  python:
    - module: ./chunkers.py
      function: gather_samples
      mode: unsafe
      timeout: 30
    - module: ./chunkers.py
      function: cost_preflight
      mode: unsafe
      timeout: 10
    - module: ./chunkers.py
      function: apply_strategy
      mode: unsafe
      timeout: 300
postprocessor:
  output_schema: index_summary
  steps:
    - type: python
      module: ./chunkers.py
      function: apply_strategy
      into: data.chunk_stats
      output_schema:
        type: object
        required: [chunk_count, source_lock_acquired]
        properties:
          chunk_count:          {type: integer, minimum: 0}
          source_lock_acquired: {type: boolean}
          chunks_path:          {type: string}
    - type: run_op
      into: data.embed_result
      op:
        kind: embed
        input_artifact: artifacts/chunks.jsonl
        output_artifact: artifacts/chunks_with_vectors.jsonl
      args_from: {}
    - type: run_op
      into: data.index_result
      op:
        kind: index_write
        source: __placeholder__
        input_artifact: artifacts/chunks_with_vectors.jsonl
        mode: append
      args_from:
        source: data.source
        mode: data.mode
        description: data.description
        path: data.path
---

## Overview

`index_docs` builds a searchable semantic index over files matching a path glob.
It is the entry point for Reyn's RAG infrastructure (ADR-0033 Phase 1) and
uses the Skill.postprocessor mechanism to run a deterministic pipeline after
the LLM has decided the chunking strategy.

## Execution flow

1. **Phase `strategy`** (LLM):
   - OS preprocessor runs `gather_samples` to collect ~5 file excerpts
   - OS preprocessor runs `cost_preflight` to estimate embedding cost
   - LLM decides `boundary`, `max_chunk_size_tokens`, and other strategy fields
   - LLM aborts if cost exceeds `cost_warn_threshold` (UX gap fix B)

2. **Skill.postprocessor** (deterministic, LLM not involved):
   - `apply_strategy` (python step, trusted): reads files, splits into chunks
     per strategy, acquires source-level advisory lock (UX gap fix D), writes
     `artifacts/chunks.jsonl` to the workspace
   - `embed` (run_op): reads `chunks.jsonl`, embeds via LiteLLM, writes
     `artifacts/chunks_with_vectors.jsonl` (progress events = UX gap fix C)
   - `index_write` (run_op): reads `chunks_with_vectors.jsonl`, writes to
     `SqliteIndexBackend`, updates `SourceManifest`

## Input

```
reyn run index_docs '{"source": "my_docs", "path": "docs/**/*.md", "description": "My project documentation"}'
```

Or with the explicit `type` / `data` envelope (= `reyn run` accepts both
shapes; the bare-data form is auto-wrapped):

```
reyn run index_docs '{"type": "index_docs_input", "data": {"source": "my_docs", "path": "docs/**/*.md", "description": "My project documentation"}}'
```

A future `reyn source index --source ... --path ... --description ...`
flag-form CLI wrapper is tracked as carry-over but not implemented in 1.0.

## Output

`index_summary` with:
- `source` — the indexed source name
- `chunk_count` — total chunks produced
- `embedded_count` / `skipped_count` — embed op results
- `written_count` — chunks written to the index backend
- `boundary` / `max_chunk_size_tokens` — strategy used

## Override pattern (ADR-0033 §2.1)

Override the chunkers module for project-specific file formats:

```yaml
# reyn/project/index_python_src/skill.md
extends: stdlib/index_docs

phases:
  strategy:
    instructions_override: |
      Python AST chunking — split at function/class boundaries...

postprocessor:
  steps:
    - type: python
      module: ./ast_chunkers.py   # project-local Python AST chunker
      function: apply_strategy
      into: data.chunk_stats
      output_schema: ...
    # embed + index_write steps unchanged (inherited)
```

## Cost preflight (UX gap fix B)

The LLM is shown `data.cost.estimated_cost_usd` and `data.cost.threshold_exceeded`
before deciding. If cost is unexpectedly high, the LLM aborts and the
postprocessor does not run — no API calls are made.

## Concurrent lock (UX gap fix D)

`apply_strategy` acquires `.reyn/index/<source>/.lock` before processing.
Concurrent `index_docs` runs for the same source are rejected with a
`SourceLockedError` (= clear error message listing the holder PID).

## Progress feedback (UX gap fix C)

The `embed` op handler emits `embed_progress` events per batch, available
in the event log and surfaced by the TUI's cost/progress tab.
