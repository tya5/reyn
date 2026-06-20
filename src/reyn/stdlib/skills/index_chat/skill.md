---
type: skill
name: index_chat
description: |
  Index past conversation turns from the chat event log for semantic search
  (#1821 improvement-1).

  Phase 1 (LLM): resolve chat cursor + summarise chat file inventory.
  Phase 2 (Skill.postprocessor): deterministic chunk → provider-direct
  embed+index pipeline; LLM is not involved.

  Scans ``.reyn/events/agents/<name>/chat/**/*.jsonl`` for
  ``user_message_received`` events.  Each user turn becomes one searchable
  chunk in the ``"chat"`` RAG source.  Incremental via
  ``.reyn/index/chat_cursor`` — separate from the events cursor so
  ``index_events`` and ``index_chat`` can run independently without interfering.

  Use ``recall --sources chat`` (or ``sources: ["chat"]`` in a recall op) to
  query past conversation history.
entry: scan
final_output: chat_scan_plan
final_output_description: |
  LLM-contract artifact: echoes back the resolved since timestamp, chat file
  inventory summary (count + ts range), and mode.  The skill postprocessor
  re-globs chat files deterministically and uses ``since`` to run the
  deterministic chunk → provider-direct embed+index pipeline.
finish_criteria:
  - Preprocessor-resolved scan context was reviewed
  - chat_scan_plan artifact echoes since, chat_files_count, and mode
search_hints:
  - "index my chat history for recall"
  - "make past conversations searchable"
  - "build semantic index of conversation turns"
  - "index chat messages for recall queries"
graph:
  scan: []
permissions:
  python:
    - module: ./chunkers.py
      function: resolve_chat_scan_context
      mode: safe
      timeout: 30
    - module: ./chunkers.py
      function: run_collect_chat_chunks
      mode: safe
      timeout: 300
    - module: ./chunkers.py
      function: run_advance_chat_cursor
      mode: safe
      timeout: 10
postprocessor:
  output_schema: index_chat_summary
  steps:
    # Step 1: walk .reyn/events/agents/ chat JSONL files, extract user turns,
    # and stream them into reyn.api.safe.embed_index (provider-direct
    # embed+index to the "chat" source).
    - type: python
      module: ./chunkers.py
      function: run_collect_chat_chunks
      into: data.chat_chunk_stats
      mode: safe
      output_schema:
        type: object
        required: [chunk_count, skipped_turns]
        properties:
          chunk_count:    {type: integer, minimum: 0}
          skipped_turns:  {type: integer, minimum: 0}
          embedded:       {type: integer, minimum: 0}
          skipped_embed:  {type: integer, minimum: 0}
          written:        {type: integer, minimum: 0}
          skipped_write:  {type: integer, minimum: 0}
          max_turn_ts:    {type: string}
    # Step 2: advance .reyn/index/chat_cursor to the max turn timestamp of
    # the indexed batch.
    - type: python
      module: ./chunkers.py
      function: run_advance_chat_cursor
      into: data.chat_cursor_result
      mode: safe
      output_schema:
        type: object
        required: [indexed_turns, new_cursor, sources_updated]
        properties:
          indexed_turns:   {type: integer, minimum: 0}
          skipped_turns:   {type: integer, minimum: 0}
          new_cursor:      {type: string}
          sources_updated: {type: array, items: {type: string}}
required_credentials: []
---

## Overview

`index_chat` indexes past conversation turns (user messages) from the chat
event log into the `"chat"` RAG source, enabling semantic search over
conversation history via `recall`.

Each `user_message_received` event becomes one chunk.  Turn-outcome metadata
(`inline_reply`, `routing`, `spawned`) is annotated on the chunk so recall
results carry context about what the user's message triggered.

Incremental via `.reyn/index/chat_cursor` — a cursor **separate** from
`.reyn/index/events_cursor` used by `index_events`.  Running both skills
independently is safe and recommended: `index_events` handles skill-run history,
`index_chat` handles conversation history.

## Execution flow

1. **Phase `scan`** (LLM):
   - OS preprocessor runs `resolve_chat_scan_context` to read the chat cursor +
     summarise the chat file inventory (count + ts range; full path list NOT
     exposed to LLM)
   - LLM echoes the resolved `since`, `chat_files_count`, and `mode` into the
     `chat_scan_plan` artifact and finishes immediately

2. **Skill.postprocessor** (deterministic, LLM not involved):
   - `run_collect_chat_chunks` (python step, safe): walks
     `.reyn/events/agents/*/chat/**/*.jsonl`, extracts `user_message_received`
     events since the cursor, and **streams** the chunks into
     `reyn.api.safe.embed_index.embed_and_index` — which embeds them
     provider-direct and writes vectors to the `chat` index source
     (`.reyn/index/chat/index.db`), tracking the max `turn_ts`
   - `run_advance_chat_cursor` (python step, safe): writes the max `turn_ts`
     (from `data.chat_chunk_stats`) to `.reyn/index/chat_cursor`

## Input

```
reyn run index_chat
reyn run index_chat --input '{"since": "2026-06-01T00:00:00"}'
reyn run index_chat --input '{"mode": "replace"}'
```

## Output

`index_chat_summary` with:
- `indexed_turns` — user turns indexed in this invocation
- `skipped_turns` — turns skipped (no user_message_received event)
- `new_cursor` — ISO timestamp written to `.reyn/index/chat_cursor`

## Recall pattern

```yaml
- op: recall
  query: "how does recall work?"
  sources: ["chat"]
  top_k: 10
```

Or cross-source search (events + chat combined):

```yaml
- op: recall
  query: "index_events failure"
  sources: ["events", "chat"]
  top_k: 10
```
