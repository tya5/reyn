---
type: skill
name: chat_compactor
description: |
  Fold a chunk of chat history into a structured rolling summary that fits
  within token budgets. Used by ChatSession to keep long sessions bounded
  while preserving the most important context per the Head/Body/Tail
  compaction strategy (see PR4 in the Reyn architecture plan).
entry: compact
final_output: chat_summary_raw
final_output_description: |
  LLM-contract artifact: structured section content + a verbatim
  `new_turn_seqs` list. The skill postprocessor takes `max()` of that
  list to derive `covers_through_seq` and emits the caller-facing
  `chat_summary` artifact (which ChatSession appends to history.jsonl).
finish_criteria:
  - All new_turns are reflected in the appropriate sections
  - No section blatantly exceeds its token cap
  - new_turn_seqs is the verbatim list of seq values from new_turns
permissions:
  python:
    - module: ./postprocessor.py
      function: compute_covers_through_seq
      mode: safe
      timeout: 5
postprocessor:
  output_schema: chat_summary
  output_name: chat_summary
  steps:
    - type: python
      module: ./postprocessor.py
      function: compute_covers_through_seq
      into: data
      output_schema:
        type: object
        required: [topic_arc, covers_through_seq]
        properties:
          topic_arc:            {type: string}
          decisions:            {type: array, items: {type: string}}
          pending:              {type: array, items: {type: string}}
          session_user_facts:   {type: array, items: {type: string}}
          artifacts_referenced: {type: array, items: {type: string}}
          covers_through_seq:   {type: integer, minimum: 0}
graph:
  compact: []
---

## Overview

Single-phase skill invoked from `ChatSession._maybe_compact()` when the
chat history's uncovered middle portion exceeds `chat.compaction.trigger_total_tokens`.
Folds new raw turns into the previous summary, updating each section
per its retention rules. The output is appended to history.jsonl as a
`role: "summary"` entry; the slicer picks up the most recent one.

## Input / Output

- Input: `history_chunk_to_compact` with `previous_summary` (optional)
  and `new_turns` (oldest first) and `section_token_caps`.
- LLM output: `chat_summary_raw` — structured sections + verbatim
  `new_turn_seqs` list.
- Caller-facing output: `chat_summary` — same sections plus
  `covers_through_seq` (= `max(new_turn_seqs)`), derived deterministically
  by the skill postprocessor.

## Notes

- This skill is invoked from chat sessions, not the CLI. `reyn run chat_compactor '...'`
  works for testing if you manually construct the input artifact.
- Compaction is best-effort: section_token_caps are soft (LLM may slightly
  exceed). The body_token_cap config exists as the aggregate limit;
  meaningful overrun should be rare and self-correcting on next compaction.
- The compactor is excluded from `available_skills` for the chat router
  (same as skill_router itself) — it is internal infrastructure.
- `covers_through_seq` is derived by the postprocessor (not the LLM)
  because getting it wrong causes turn duplication or loss in
  ChatSession.history. See `postprocessor.py` for the derivation rules.
