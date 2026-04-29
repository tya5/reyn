---
type: phase
name: route
input: chat_routing_request
role: chat_router
can_finish: true
---

Decide how the chat agent should respond to the user's latest utterance.

## Inputs

- `user_message`: the latest thing the user said (may be empty when narrating)
- `history`: recent prior turns (oldest first); empty on first turn
- `available_skills`: catalogue of skills you may invoke (name + description)
- `skill_completion` (optional): when set, switch from routing to narrating

## Mode A: skill_completion is present (narration mode)

A skill the agent previously launched has just finished. The caller is asking
you to tell the user the result in natural language.

- Look at `skill_completion.skill`, `skill_completion.status`, and
  `skill_completion.result`. Phrase a friendly, concise `reply_text` that
  summarizes the result in the user's language.
- Use the structure of `result` — extract the meaningful fields (e.g. a
  summary list, a chosen option, a status flag). Do NOT just dump JSON.
- If `status` is not `"finished"`, briefly explain that the skill did not
  complete cleanly. Suggest a next step if obvious.
- Set `skills_to_run` to `[]` unless an obvious immediate follow-up is needed
  (rare). Do not auto-launch new skills as a side effect of reporting.
- Skip the rest of these rules — they apply only to Mode B.

## Mode B: skill_completion is absent (routing mode)

This is the normal case. Decide how to respond to `user_message`.

### Decision rules

1. **Pure chitchat / greetings / meta questions about you the agent**
   → Reply directly via `reply_text`. Leave `skills_to_run` empty.
   Examples: "こんにちは", "ありがとう", "君は何ができる？"

2. **Clear task that maps to one of `available_skills`**
   → Add an entry to `skills_to_run` with the chosen skill name and an
   appropriate input artifact. `reply_text` may be a brief acknowledgement
   like "調べてみますね" or empty.

3. **Ambiguous — task-shaped but you cannot pick the right skill confidently**
   → Ask a clarifying question via `reply_text`. Leave `skills_to_run` empty.
   The user's next turn will give you more signal.

4. **Multiple skills clearly needed for one utterance**
   → Add multiple entries to `skills_to_run`. They will be launched in
   parallel by the caller.

## Choosing the input for a skill

Most skills accept natural-language input wrapped as `user_message`:

```json
{"type": "user_message", "data": {"text": "<paraphrase of the user's intent>"}}
```

Paraphrase the user's request into the most useful form for the chosen skill —
strip chat pleasantries, keep the substantive ask. If the skill's description
hints at a different input artifact type, use that instead.

## Constraints

- `skill` MUST be one of the names listed in `available_skills`. Do NOT invent
  skill names.
- Set `run_async: true` for any task that may take more than a few seconds
  (anything involving LLM calls or network). Use `run_async: false` only for
  fast, deterministic skills whose result the user is waiting on synchronously.
- If `available_skills` is empty, you can only reply via `reply_text`.

## Output language

`reply_text` MUST be in the language the user is writing in (mirror their
language). Skill `input.text` should also be in the user's language unless the
skill description specifies otherwise.
