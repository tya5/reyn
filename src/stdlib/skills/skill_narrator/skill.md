---
type: skill
name: skill_narrator
description: |
  Convert a finished skill's structured `final_output` into a chat-friendly
  natural-language reply. ChatSession invokes this internally after every
  skill spawn; the resulting `reply_text` is shown to the user and persisted
  to chat history.
entry: narrate
final_output: narration_result
final_output_description: |
  A short natural-language sentence (or two) describing what the skill did,
  in the user's language. The reply is streamed verbatim to the chat user
  and appended to history as `role=agent`.
finish_criteria:
  - reply_text is non-empty when status is "finished"
  - reply_text concisely describes what completed (no JSON dumps)
  - reply_text explains the failure mode when status ≠ "finished"
graph:
  narrate: []
routing:
  intents: []
  when_to_use:
    - "(internal) Called only by ChatSession after a skill spawn completes."
  when_not_to_use:
    - "User-facing routing — the router excludes this skill from its catalogue."
---

## Overview

Single-phase translation skill. Given a finished skill's structured output
plus its run status, produce a chat-friendly reply. Replaces the previous
"call skill_router with `skill_completion` set" pattern (PR9 split):

- `skill_router` is now routing-only
- `skill_narrator` is narration-only
- ChatSession picks which to invoke based on the situation (user input vs
  skill completion)

## Input

`narration_request` artifact with:

- `skill`: name of the skill that just finished
- `status`: `"finished"` | `"loop_limit_exceeded"` | other terminal statuses
- `result`: the skill's final_output `data`, as-is (structured JSON)

## Output

`narration_result` artifact:

- `reply_text`: short natural-language description of what happened. Use
  the user's language. Do NOT dump JSON. For non-finished statuses, briefly
  explain what didn't complete and suggest a next step if obvious.

## Notes

- The router excludes this skill from its catalogue (`available_skills`)
  so the LLM never picks it for user-facing routing — it's invoked only by
  ChatSession's post-skill-spawn path.
- Keep the reply terse: one or two short sentences usually suffice.
  ChatSession persists this verbatim to history as `role=agent`, where it
  also feeds back into the next router turn's context, so over-long
  narrations bloat downstream prompts.
