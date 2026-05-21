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
- `new_turns` — recent raw turns to absorb (oldest first). Each turn is
  `{role, text, seq, ...}` where `role` is one of:
    - `user` / `assistant` — regular conversational turns.
    - `assistant` (with `tool_calls`) — the LLM decided to invoke one or
      more tools; the entry carries
      `tool_calls: [{name, args_chars}, ...]` so you can reason about
      what was called (= source for `artifacts_referenced`).
    - `tool` (with `tool_call_id` + `tool_name`) — the tool's response
      to a specific `tool_call`; `text` is the JSON-serialised result.
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

  Include items derived from **tool activity** in `new_turns` when
  they are conversation-relevant:
    - `file_read` / `file_write` / `file_edit` ops → "edited
      <path>" / "read <path>" entries.
    - `web_fetch` ops → "fetched <url>" entries.
    - `mcp.tool__<server>.<tool>` ops → "called <server>.<tool>"
      entries when the call result informed the user-visible reply.
  Do NOT enumerate every tool call mechanically — only those whose
  outputs the conversation depends on going forward (= same
  retention rule as other artifacts).

## Output

Produce a `chat_summary_raw` artifact with all sections filled in. Set
`new_turn_seqs` to a **verbatim list of every `seq` value** taken from
the input `new_turns`. Do NOT compute the maximum yourself, do NOT sort,
dedupe, or filter — just copy each `seq` from each entry of `new_turns`,
in order.

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

- Output ONLY the `chat_summary_raw` artifact (decide turn). Do NOT emit ops.
- `new_turn_seqs` MUST contain every seq from `new_turns`, copied verbatim.
  The skill postprocessor takes `max()` of this list to derive
  `covers_through_seq`. If you omit a seq the slicer may re-include the
  corresponding turn (duplication); if you fabricate a higher seq the
  slicer will skip turns that have not been folded into a summary (loss).
- Do NOT include raw quotes from new_turns in the summary unless they
  are the verbatim text of a decision or pending item. Compaction is
  meant to abstract, not transcribe.
