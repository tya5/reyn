---
type: skill
name: skill_router
description: |
  Route a single user chat utterance to an appropriate skill (or reply directly).
  Used by `reyn chat` to turn natural language into a routing decision: zero or more
  skills to invoke, plus an optional immediate text reply.
entry: triage
final_output: routing_decision
final_output_description: |
  Decision describing how to respond to the user's utterance: an optional
  conversational `reply_text`, plus zero or more skills to invoke with their inputs.
finish_criteria:
  - Triage classifies the utterance into one of four bucket intents
    (chitchat / task / fresh_lookup / direct_reply) and routes accordingly
  - chitchat / direct_reply intents transition to `reply` for direct answer
  - task intents transition to `match` for skill / agent selection
  - fresh_lookup intents transition to `match` (which forwards to web_research)
graph:
  triage: [match, reply]
  match: [web_research]
  web_research: []
  reply: []
---

## Overview

Implicit skill invocation engine for the chat agent. Given a single user
utterance, recent conversation history, and the catalogue of available skills,
decide how to respond.

### Three-phase design (triage → {match, reply})

PR34 split. Earlier versions packed six fine-grained intents into a single
`classify` phase; weak models routinely mis-classified greetings and short
acks as task intents because the catalogue's writer-style skills bias the
LLM toward post-hoc rationalisation. The current design narrows each
phase's decision space:

1. **triage** — pick exactly one of four bucket intents:

   | Intent          | Means                                                            | Next phase  |
   |-----------------|------------------------------------------------------------------|-------------|
   | `chitchat`      | Greeting / thanks / ack / casual social ping. No work requested. | `reply`     |
   | `task`          | A skill or peer agent could plausibly fulfil this.               | `match`     |
   | `fresh_lookup`  | Needs current / time-sensitive web data the model can't recall.  | `match`     |
   | `direct_reply`  | Memory recall / stable knowledge / clarification — anything that the LLM can answer directly without a skill. | `reply` |

   Triage emits a `routing_intent` artifact and transitions. It does NOT
   compose the user-facing reply itself; that's the next phase's job.

2. **match** — only reached for `task` / `fresh_lookup`. For `task` it
   consults the skill catalogue and constructs `skills_to_run` (or
   `messages_to_agents` for delegation). For `fresh_lookup` it transitions
   to the `web_research` sub-phase with a freshly-built
   `web_research_request`.

3. **reply** — only reached for `chitchat` / `direct_reply`. Composes the
   `reply_text` from history, memory_index, and stable training knowledge.
   Also handles per-turn memory writes (the user's incoming utterance is
   the source of truth that may be worth persisting).

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
  triage → {match, reply} handoff and never escapes the skill.
