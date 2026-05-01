---
type: phase
name: compact
input: history_chunk_to_compact
role: chat_compactor
can_finish: true
allowed_ops: []
max_act_turns: 1
---

Fold the new chat turns into a structured rolling summary that fits
within the per-section token budgets. Update each section by adding new
information and dropping low-importance items per the retention rules.

## Inputs

- `previous_summary` — the current rolling summary (may be null).
- `new_turns` — recent raw turns to absorb (oldest first).
- `section_token_caps` — soft per-section token budgets.

## Sections (and retention rules)

- **topic_arc**: 1-3 sentences. Update to reflect the latest topic shift.
  Drop the previous topic if the conversation has clearly moved on.
- **decisions**: bullet list of choices made. Add new decisions. Drop the
  oldest minor decisions when over budget; never drop architectural
  decisions or anything labeled as final.
- **pending**: bullet list of open items (questions, unfinished tasks,
  follow-ups). Add new ones; remove items the conversation has resolved.
- **session_user_facts**: short bullets of user attributes learned this
  session that aren't durable memory yet. Drop the oldest if over
  budget — durable attributes belong in MEMORY.md anyway.
- **artifacts_referenced**: bullets of files / PRs / commits / issues
  referenced this session. Drop ones no longer in scope.

## Output

Produce a `chat_summary` artifact with all sections filled in. Set
`covers_through_seq` to the **highest seq value among the new_turns**.
If `previous_summary` was provided, your output replaces it (the new
summary covers everything the old one did **plus** the new_turns).

The total length must stay within the budgets given by
`section_token_caps`. When over budget, remove the LEAST IMPORTANT items
first per the rules above. Never silently lose user-attribute or pending
items — drop them only when their retention condition is met (memory
promotion / item resolution / focus shift).

Match the user's language for free-text fields (topic_arc, decisions
bullets, etc.) — infer language from the new_turns content.

## Constraints

- Output ONLY the `chat_summary` artifact (decide turn). Do NOT emit ops.
- `covers_through_seq` MUST equal the largest seq value seen in
  `new_turns`. Setting it lower causes the slicer to re-include those
  turns, defeating the compaction.
- Do NOT include raw quotes from new_turns in the summary unless they
  are the verbatim text of a decision or pending item. Compaction is
  meant to abstract, not transcribe.
