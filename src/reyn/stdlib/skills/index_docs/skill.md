---
type: skill
name: index_docs
description: |
  Build a searchable semantic index over a path glob (ADR-0033 §2.1).

  Phase 1 (LLM): inspect file samples + cost preflight, decide a chunk
  strategy, or abort if cost is unexpectedly high.
  Phase 2 (Skill.postprocessor): deterministic chunk → provider-direct
  embed+index pipeline; LLM is not involved.

  Override the chunkers module for project-specific formats (Python AST,
  custom Markdown, etc.) via `extends: stdlib/index_docs` + module override.
entry: strategy
final_output: chunk_strategy
final_output_description: |
  LLM-contract artifact: chunking strategy decision (boundary, chunk size,
  overlap, parent context flag) plus echoed passthrough fields (source,
  path, description, mode). The skill postprocessor uses this to run the
  deterministic chunk → provider-direct embed+index pipeline.
finish_criteria:
  - File samples and cost preflight were reviewed
  - A chunk_strategy was decided (or abort was issued for high-cost inputs)
  - source, path, description, and mode are echoed in the artifact
search_hints:
  - "index my docs so I can search them"
  - "build a semantic index over these files"
  - "make my documentation searchable"
  - "index the source code for recall queries"
graph:
  strategy: []
permissions:
  python:
    # FP-0042 Phase 2.1 (2026-05-22): preprocessor steps migrated from
    # chunkers.py (mode: unsafe) → chunkers_preproc_safe.py (mode: safe).
    # File reads / stat calls go through reyn.api.safe.file, which gates
    # against the workspace default-read-zone (CWD) and any explicit
    # file.read entries below.
    - module: ./chunkers_preproc_safe.py
      function: gather_samples
      mode: safe
      timeout: 30
    - module: ./chunkers_preproc_safe.py
      function: cost_preflight
      mode: safe
      timeout: 10
    - module: ./chunkers_safe.py
      function: extract_and_split
      mode: safe
      timeout: 30
    # #1303 Stage I (2026-06-04): write_chunks_with_lock streams chunks
    # straight into reyn.api.safe.embed_index (provider-direct embed+index) —
    # no intermediate JSONL file. Source reads go through reyn.api.safe.file;
    # PID identity + liveness through reyn.api.safe.process; embed+index writes
    # land under .reyn/index/<source>/ (default write zone). No out-of-zone
    # file.write grant is needed (the old `artifacts` grant is dropped).
    - module: ./chunkers_safe.py
      function: write_chunks_with_lock
      mode: safe
      timeout: 300
postprocessor:
  output_schema: index_summary
  steps:
    # Step 1: safe — glob enum (path list only, no file content read).
    - type: python
      module: ./chunkers_safe.py
      function: extract_and_split
      into: data.chunk_list
      mode: safe
      output_schema:
        type: array
        items:
          type: object
          required: [source_path]
          properties:
            source_path: {type: string}
    # Step 2: safe — lock + file content read + provider-direct embed+index
    # (streams chunks into reyn.api.safe.embed_index; folds the old embed +
    # index_write run-ops, no intermediate file). #1303 Stage I.
    - type: python
      module: ./chunkers_safe.py
      function: write_chunks_with_lock
      into: data.chunk_stats
      mode: safe
      output_schema:
        type: object
        required: [chunk_count, source_lock_acquired]
        properties:
          chunk_count:          {type: integer, minimum: 0}
          source_lock_acquired: {type: boolean}
          embedded:             {type: integer, minimum: 0}
          skipped_embed:        {type: integer, minimum: 0}
          written:              {type: integer, minimum: 0}
          skipped_write:        {type: integer, minimum: 0}
# FP-0016 D: this skill needs no static secrets / OAuth tokens.
required_credentials: []
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
   - `extract_and_split` (python step, safe): enumerates source files via
     glob; no file content read
   - `write_chunks_with_lock` (python step, safe): acquires the source-level
     advisory lock (UX gap fix D), reads each source file via `reyn.api.safe.file`,
     splits into chunks per strategy, and **streams** the chunks into
     `reyn.api.safe.embed_index.embed_and_index` — which embeds them provider-direct
     and writes the vectors to `SqliteIndexBackend`
     (`.reyn/index/<source>/index.db`), then updates `SourceManifest`. No
     intermediate file (#1303 Stage I folds the old `embed` + `index_write`
     run-ops into this step). Resume = DB-as-checkpoint: already-indexed
     `content_hash`es are skipped before embedding.

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
- `chunk_stats` — the write_chunks_with_lock result:
  - `chunk_count` — total chunks produced (= `embedded` + `skipped_embed`)
  - `embedded` / `skipped_embed` — newly embedded vs skipped pre-embed (resume)
  - `written` / `skipped_write` — written to the index vs dup `content_hash`
  - `source_lock_acquired` — whether the source lock was held
- `boundary` / `max_chunk_size_tokens` — strategy used

## Override pattern (ADR-0033 §2.1)

Override the chunker modules for project-specific file formats by replacing
the two-step chain. Both override steps should declare `mode: safe` and use
the `reyn.api.safe.file` API for any file I/O (= the FP-0042 stdlib safe-only
doctrine applies to project chunkers too):

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
      module: ./ast_chunkers.py   # project-local Python AST enumerator
      function: extract_and_split
      into: data.chunk_list
      mode: safe
      output_schema: ...
    - type: python
      module: ./ast_chunkers.py
      function: write_chunks_with_lock
      into: data.chunk_stats
      mode: safe
      output_schema: ...
    # embed+index happen inside write_chunks_with_lock (it streams chunks
    # into reyn.api.safe.embed_index) — no separate embed / index_write steps.
```

A project chunker override should call
`reyn.api.safe.embed_index.embed_and_index(chunks, source, "standard", mode=...,
description=..., path=...)` from its `write_chunks_with_lock` so the
provider-direct embed+index, DB-checkpoint resume, and SourceManifest upsert
are preserved. The pre-FP-0042 single-step `apply_strategy` override path was
retired in Phase 2.8.

## Cost preflight (UX gap fix B)

The LLM is shown `data.cost.estimated_cost_usd` and `data.cost.threshold_exceeded`
before deciding. If cost is unexpectedly high, the LLM aborts and the
postprocessor does not run — no API calls are made.

## Concurrent lock (UX gap fix D)

`write_chunks_with_lock` acquires `.reyn/index/<source>/.lock` before
processing. Concurrent `index_docs` runs for the same source are rejected
with a `SourceLockedError` (= clear error message listing the holder PID).

## Progress feedback (UX gap fix C)

Per-batch `embed_progress` events came from the old `embed` op. With embedding
folded into the safe-mode `write_chunks_with_lock` step (which runs in the
python-harness subprocess), per-batch progress does not currently cross the
subprocess boundary — tracked as a follow-up (#1306). Phase-1 `cost_preflight`
(the cost the LLM approves up front) is unchanged.
