---
type: skill
name: skill_router
description: |
  Route a single user chat utterance to an appropriate skill (or reply directly).
  Used by `reyn chat` to turn natural language into a routing decision: zero or more
  skills to invoke, plus an optional immediate text reply.
entry: classify
final_output: routing_decision
final_output_description: |
  Decision describing how to respond to the user's utterance: an optional
  conversational `reply_text`, plus zero or more skills to invoke with their inputs.
finish_criteria:
  - The user's intent has been classified into a closed-vocabulary label
  - Easy intents (chitchat / memory_recall / stable_knowledge / narrate / clarification)
    finish in classify with reply_text directly
  - Task intents transition to match for skill selection
  - Fresh-lookup intents transition through match to web_research
graph:
  classify: [match, web_research]
  match: [web_research]
  web_research: []
---

## Overview

Implicit skill invocation engine for the chat agent. Given a single user
utterance, recent conversation history, and the catalogue of available skills,
decide how to respond.

### Two-phase design (classify → match)

1. **classify** — assign one of seven closed-vocabulary intents
   (`narrate`, `task`, `fresh_lookup`, `chitchat`, `memory_recall`,
   `stable_knowledge`, `clarification`) using specificity-first ordering.
   Five of those intents (everything except `task` and `fresh_lookup`)
   finish in classify with `reply_text` — this is the **fast path**, one
   LLM call per turn for the common case.
2. **match** — only reached when classify chose `task` or `fresh_lookup`.
   For `task`, it consults the skill catalogue and constructs
   `skills_to_run`. For `fresh_lookup`, it transitions to the
   `web_research` sub-phase with a freshly-built `web_research_request`.

The router does NOT execute the skills — it returns the routing decision;
the caller (ChatSession) launches them asynchronously.

## Input

`chat_routing_request` artifact with:

- `user_message`: the latest user utterance
- `history`: recent `{role, text}` pairs (preprocessor-injected)
- `available_skills`: `[{name, description, routing?}]` from project / local
  / stdlib catalogue. Each entry may include an opt-in `routing` block
  (`intents`, `when_to_use`, `when_not_to_use`, `examples`) to help the
  router pick correctly. stdlib skills ship with `routing` blocks.

## Output

`routing_decision` artifact:

- `reply_text`: string — empty when only skill invocation is appropriate
- `skills_to_run`: list of `{skill, input, run_async}` — empty for pure
  chitchat / direct replies

## Notes

- Invoked from chat sessions; not typically run from the CLI
  (`reyn run skill_router '...'` works for testing if the input is
  manually constructed as a `chat_routing_request`).
- The router itself is excluded from `available_skills` to prevent recursion.
- The intermediate `routing_intent` artifact is internal to the
  classify→match handoff and never escapes the skill.
