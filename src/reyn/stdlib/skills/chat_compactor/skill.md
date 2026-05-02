---
type: skill
name: chat_compactor
description: |
  Fold a chunk of chat history into a structured rolling summary that fits
  within token budgets. Used by ChatSession to keep long sessions bounded
  while preserving the most important context per the Head/Body/Tail
  compaction strategy (see PR4 in the Reyn architecture plan).
entry: compact
final_output: chat_summary
final_output_description: |
  Updated rolling summary covering the new turns plus everything covered
  by the previous summary (if any). Replaces the previous summary in
  history.jsonl when ChatSession appends it.
finish_criteria:
  - All new_turns are reflected in the appropriate sections
  - No section blatantly exceeds its token cap
  - covers_through_seq equals the highest seq in new_turns
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
- Output: `chat_summary` with structured sections + `covers_through_seq`.

## Notes

- This skill is invoked from chat sessions, not the CLI. `reyn run chat_compactor '...'`
  works for testing if you manually construct the input artifact.
- Compaction is best-effort: section_token_caps are soft (LLM may slightly
  exceed). The body_token_cap config exists as the aggregate limit;
  meaningful overrun should be rare and self-correcting on next compaction.
- The compactor is excluded from `available_skills` for the chat router
  (same as skill_router itself) — it is internal infrastructure.
