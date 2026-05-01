---
type: skill
name: skill_router
description: |
  Route a single user chat utterance to an appropriate skill (or reply directly).
  Used by `reyn chat` to turn natural language into a routing decision: zero or more
  skills to invoke, plus an optional immediate text reply.
entry: route
final_output: routing_decision
final_output_description: |
  Decision describing how to respond to the user's utterance: an optional
  conversational `reply_text`, plus zero or more skills to invoke with their inputs.
finish_criteria:
  - The user's intent has been classified
  - reply_text is set when a conversational answer alone suffices
  - skills_to_run is populated when one or more skills are appropriate
  - Web-lookup questions transition to web_research, which produces the same routing_decision
graph:
  route: [web_research]
  web_research: []
---

## Overview

Implicit skill invocation engine for the chat agent. Given a single user utterance,
recent conversation history, and the catalogue of available skills, decide:

- whether to reply directly with text (chitchat, clarification, status)
- whether to invoke one or more skills (task delegation)
- both can happen in the same turn (e.g. acknowledge while a skill runs)

The router does NOT execute the skills — it returns the routing decision; the
caller (ChatSession) launches them asynchronously.

## Input

`chat_routing_request` artifact with:

- `user_message`: the latest user utterance
- `history`: recent `{role, text}` pairs (may be empty for first turn)
- `available_skills`: `[{name, description}]` from project / local / stdlib catalogue

## Output

`routing_decision` artifact:

- `reply_text`: string — empty if no direct reply is needed
- `skills_to_run`: list of `{skill, input, run_async}` — empty for pure chitchat

## Notes

- This skill is invoked from chat sessions; it is not typically run from the CLI
  (though `reyn run skill_router '...'` works for testing if the input is
  manually constructed as a `chat_routing_request`).
- The router itself is excluded from `available_skills` to prevent recursion.
