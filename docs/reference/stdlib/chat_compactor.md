---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [chat_compactor]
---

# `chat_compactor`

Fold a chunk of chat history into a structured rolling summary that fits within token budgets.

## Entry

`compact`

## Final output

`chat_summary` — structured rolling summary with sections (`topic_arc`, `decisions`, `pending`, `session_user_facts`, `artifacts_referenced`) plus `covers_through_seq` derived deterministically by the postprocessor.

## How it composes

Single-phase skill: `compact` finishes immediately after one LLM call. The LLM produces `chat_summary_raw` (sections + verbatim `new_turn_seqs` list); the skill postprocessor then computes `covers_through_seq = max(new_turn_seqs)` in pure Python and emits the caller-facing `chat_summary` artifact. This keeps arithmetic out of the LLM contract and prevents turn duplication or loss in `ChatSession.history`.

## Caveats

- Invoked internally by `ChatSession._maybe_compact()` — not intended for direct CLI use. You can test it with `reyn run chat_compactor '...'` by constructing the `history_chunk_to_compact` input manually.
- Excluded from `available_skills` for the chat router (internal infrastructure).
- Section token caps are soft; minor overrun is expected and self-corrects on the next compaction pass.
- Requires `python` permission for `postprocessor.py:compute_covers_through_seq`.

## Usage

Not invoked directly in normal use. `ChatSession` calls it automatically when the uncovered BODY region exceeds `chat.compaction.trigger_total_tokens`. For testing:

```bash
reyn run chat_compactor '{"type":"history_chunk_to_compact","data":{...}}'
```

## Source

[`src/stdlib/skills/chat_compactor/skill.md`](https://github.com/tya5/reyn/blob/main/src/stdlib/skills/chat_compactor/skill.md)
