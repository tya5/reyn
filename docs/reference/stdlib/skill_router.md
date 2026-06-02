---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_router]
---

# `skill_router`

Route a single user (or peer-agent) utterance to an appropriate skill, agent, or direct reply. Used by `reyn chat` on every turn.

## Phases

The router runs as a **two-phase** workflow:

1. **`classify`** — pick the intent (chitchat / memory_recall / stable_knowledge / clarification / task / fresh_lookup) and either finish with `routing_decision` or hand off to `match`.
2. **`match`** — for `task` and `fresh_lookup` intents, dispatch to a specific skill (or peer agent), or transition to `web_research`.

The classify phase also handles **memory writes**: every turn it inspects the new utterance and may emit `file/write` ops to persist user / feedback / project / reference facts. Memory reads are pre-merged by ChatSession (see [concepts/memory](../../concepts/data-retrieval/memory.md)).

## Entry artifact: `chat_routing_request`

ChatSession constructs this on every turn. Selected fields:

| Field | Origin | Description |
|-------|--------|-------------|
| `user_message` | inbox payload | The latest utterance to route |
| `chat_id` | session | The agent's own name (used by classify when building memory paths) |
| `history_path` | session | Path to `history.jsonl`, sliced by the classify preprocessor |
| `compaction` | config | Window-first token-budget compaction policy (`component_weights` / `section_weights`) |
| `available_skills` | session | Project + stdlib catalogue (router/compactor excluded), filtered by `profile.allowed_skills` if set |
| `available_agents` | registry | Other agents reachable via topology rules — `[{name, role}, ...]` |
| `memory_index` | session | Pre-merged shared + agent layers (`{status, content}`); `content` is markdown with `(shared)` and `(agent: <name>)` sections |

## Final output: `routing_decision`

| Field | Type | Purpose |
|-------|------|---------|
| `reply_text` | string (optional) | Text shown directly to the user — interim acknowledgement or final answer |
| `skills_to_run` | array (optional) | Project / stdlib skills to spawn this turn |
| `messages_to_agents` | array (optional) | Delegations to peer agents — `[{to, request}, ...]` |

ChatSession dispatches each non-empty array. `reply_text` reaches the user immediately for user-initiated chains. For agent-initiated chains (a peer agent received this request), the chain enters [deferred reply](../../concepts/multi-agent/multi-agent.md#deferred-reply) when `messages_to_agents` is non-empty: the router's `reply_text` is held until every delegate responds.

## Skill selection guidance

The `match` phase prefers:

- **A specific skill** with a clear `routing.examples.positive` match — narrow well-defined tasks
- **Agent delegation** when `available_agents` shows a peer whose `role` matches the request better than any skill
- **Direct reply** when neither fits cleanly and the request is small (or `confidence < 0.6` triggers clarification)

Never both a skill AND an agent in the same decision — pick one branch.

## Source

[`src/reyn/chat/session.py`](https://github.com/tya5/reyn/blob/main/src/reyn/chat/session.py) — implemented as a built-in system skill, not a regular skill directory

## See also

- [Concepts: memory](../../concepts/data-retrieval/memory.md) — 2-tier read/write contract
- [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md) — `messages_to_agents` and chain semantics
- [Reference: profile-yaml](../dsl/profile-yaml.md) — `allowed_skills` filter
- [Reference: chat CLI](../cli/chat.md)
