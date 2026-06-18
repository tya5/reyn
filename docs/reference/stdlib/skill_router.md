---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_router]
---

# `skill_router`

Route a single user (or peer-agent) utterance to an appropriate skill, agent, or direct reply. Used by `reyn chat` on every turn.

## Implementation

The router is implemented as a native-tools `RouterLoop` (not a regular skill directory with LLM phase transitions). On each turn `RouterLoop` receives the conversation history and a set of native tools representing the available dispatch paths (skill execution, agent delegation, direct reply). The LLM picks one or more tools; the loop runs until the turn is resolved.

Memory writes (persisting user / feedback / project / reference facts) happen inside the loop when the LLM emits `file/write` ops. Memory reads are pre-merged by Session before the loop runs (see [concepts/memory](../../concepts/data-retrieval/memory.md)).

## Source

[`src/reyn/chat/router_loop.py`](https://github.com/tya5/reyn/blob/main/src/reyn/chat/router_loop.py) — implemented as a built-in system skill, not a regular skill directory

## See also

- [Concepts: memory](../../concepts/data-retrieval/memory.md) — 2-tier read/write contract
- [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md) — `messages_to_agents` and chain semantics
- [Reference: profile-yaml](../dsl/profile-yaml.md) — `allowed_skills` filter
- [Reference: chat CLI](../cli/chat.md)
