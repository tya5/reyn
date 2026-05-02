---
type: phase
name: narrate
input: narration_request
role: narrator
allowed_ops: []
---

Convert the finished skill run described in `data` into a short, chat-friendly
natural-language reply for the user.

## Inputs

- `data.skill`: name of the skill that just finished
- `data.status`: terminal status (`"finished"`, `"loop_limit_exceeded"`, etc.)
- `data.result`: the skill's `final_output.data` — structured JSON

## What to produce

Emit a `narration_result` artifact whose `reply_text` is:

- **One or two short sentences**, in the user's language (output_language).
- **Domain-meaningful**: extract the fields that matter to the user from
  `data.result` — e.g. for a skill_builder result, mention the skill name and
  where it was written; for a summarizer, mention the title or topic; for a
  search, mention how many candidates were found and which one was chosen.
- **Never dump JSON**. Do not surface raw `data.result` keys verbatim.
- **Status-aware**:
  - `status == "finished"` → confirm completion concisely. Optionally hint at
    the natural next step ("〜 を試したいときは `reyn run …`" 等)、but only
    when the next step is obvious from `data.result`.
  - `status == "loop_limit_exceeded"` → say the skill ran out of phase budget
    before finishing; suggest re-running with `--max-phase-visits` raised
    *if* the user might know that flag.
  - other statuses → describe what didn't complete and suggest the most
    likely fix (re-run, check logs, etc.).

## Style

- Match the conversational register established in chat history. Keep it
  human, not robotic.
- Avoid repeating the user's own words back at them.
- Skip the skill name in the reply when it's obvious from context — the
  ChatSession renderer already shows `[skill#abcd]` provenance separately.
- Brevity matters: this `reply_text` becomes part of subsequent router
  turns' context. Long narrations bloat downstream prompts.
