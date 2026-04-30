---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_router]
---

# `skill_router`

Route a single user chat utterance to an appropriate skill, or reply directly.

## Entry

`route`

## Final output

`routing_decision` — zero or more skills to invoke, plus an optional immediate text reply.

## How it's used

`reyn chat` calls `skill_router` for every user turn. The router:

- Decides whether the utterance is a task (route to skill) or chitchat (reply directly).
- Picks the right skill from `available_skills`.
- Paraphrases the user's intent into the chosen skill's input.

## Modes

The route phase has two modes:

1. **Routing mode** — normal case. Decide what to launch.
2. **Narration mode** — set when a skill has just finished. Phrase the result as natural language for the user.

In both modes, `relevant_memories` (recalled by a separate preprocessor step) shape tone and content.

## Source

[`src/stdlib/skills/skill_router/skill.md`](https://github.com/<org>/reyn/blob/main/src/stdlib/skills/skill_router/skill.md)
