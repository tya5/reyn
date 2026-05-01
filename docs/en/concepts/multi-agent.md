---
type: concept
topic: architecture
audience: [human, agent]
---

# Multi-agent

A reyn process can host any number of long-lived **agents**, each one a ChatSession with its own profile, history, memory layer, inbox, and skill catalogue view. Agents talk to humans (one at a time, via attach) and to each other (through a structured request-response channel).

## What is an agent?

An agent is a directory at `.reyn/agents/<name>/` plus an in-memory ChatSession the runtime spins up on demand:

- `profile.yaml` — name, role (system-prompt persona), `allowed_skills` (optional)
- `history.jsonl` — append-only conversation log
- `events.jsonl` — runtime audit log
- `memory/` — agent-scoped memory layer (the shared layer at `.reyn/memory/` is visible to every agent)
- `runs/` — per-skill-spawn workspace

The `default` agent is auto-created when needed; named agents come from `reyn agent new`.

## AgentRegistry

A single `AgentRegistry` instance per process owns all loaded agents. It handles:

- **Lazy load** — agents are instantiated on first attach or first inter-agent message, not at startup.
- **Attach pointer** — exactly one agent is the REPL-attached one at a time. Detached agents keep running their inbox loop (background skill progress, intervention queues), but their transient outbox messages are dropped — only durable history persists.
- **Outbox forwarder** — a per-agent task pumps the attached agent's outbox into a shared REPL queue.
- **Topology gate** — `permit(from, to)` consults declared topologies before allowing inter-agent sends. See [topology.md](topology.md).

## Attach model

`reyn chat researcher` makes `researcher` the attached agent. While attached, `:attach default` switches the pointer back; `researcher` keeps its inbox loop running. If a delegation chain is mid-flight when you switch, you'll come back to find the resolution sitting in the outbox.

## Agent-to-agent messaging

When a router decision emits `messages_to_agents: [{to, request}, ...]`, ChatSession routes each entry to the target's inbox as an `agent_request` payload:

```
{from_agent, request, depth, chain_id}
```

The receiving agent's `session.run()` consumes it, runs its own router, and either replies immediately (`agent_response` back to the sender) or **defers** if it wants to delegate further (PR14).

### Deferred reply

If the receiving agent's router emits its own `messages_to_agents`, the upstream reply is held back. A `_PendingChain` keyed by `chain_id` records:

- `origin_agent` — who to reply to once the chain resolves
- `origin_depth` — the depth at which to send back
- `original_request` — the upstream request, replayed into the next router turn for synthesis
- `waiting_on` — set of agents whose responses are still pending

As each delegate responds, the sender is removed from `waiting_on`. When the set empties, the agent re-runs its router with all delegate responses now in history; the resulting `reply_text` becomes the single synthesized reply sent upstream. If the second router pass emits more delegations, the chain stays pending with a fresh `waiting_on` set — bounded only by `max_hop_depth`.

This gives a "manager → delegate → synthesize" model: the user sees an interim `(working on it)` from their attached agent, then a single final answer that incorporates every delegate's input.

### chain_id

Every top-level user submission mints a `chain_id` (uuid4 hex) at `submit_user_text`. It propagates verbatim through:

- inbox payloads (every hop)
- history meta on every `_append_history` involved in the chain (sources: `agent_request`, `agent_request_outgoing`, `agent_response`, `agent_response_outgoing`)
- `agent_message_*` events

`chain_id` is **audit-only** — the router LLM does not see it, the CLI does not display it. To trace a chain end-to-end across agents, `grep <chain_id>` over each agent's `events.jsonl` and `history.jsonl`.

### Fan-out

`messages_to_agents` may contain multiple entries. The pending chain's `waiting_on` set holds all of them; the synthesized reply happens only after **every** delegate responds (wait-for-all). A single slow delegate delays the whole synthesis until either it responds or `multi_agent.chain_timeout_seconds` (default 60s) elapses — at which point a `chain_timeout` event fires and a synthesized error response unblocks the upstream agent.

## User-initiated vs agent-initiated chains

The deferred-reply mechanic applies only to chains where another agent is waiting upstream. For **user-initiated** chains, the originating agent ships its router's `reply_text` to the user immediately (interim acknowledgement), then a second pass after delegate responses produces the final answer. Two visible messages, never one synthesized lump.

That preserves the existing chat UX ("you'll see I'm working on it") while letting agent-to-agent chains compose cleanly into one reply per request.

## max_hop_depth

`multi_agent.max_hop_depth` (default 3) caps how far a chain can extend. `depth = 0` is the user input; each `_send_to_agent` increments. A send with `depth > max_hop_depth` is refused with an `agent_message_refused` event. See [reference: multi-agent config](../reference/config/multi-agent.md).

## What the OS does NOT manage

- **Topology**: who can send to whom is a separate concept (see [topology.md](topology.md)) consulted by the registry's `permit()`.
- **Skill access**: the LLM-side skill filter is per-agent via `profile.allowed_skills`; the OS just respects what the profile says.
- **Memory layering**: shared vs agent layer is read/written by the router's classify phase; the registry doesn't touch memory files.

Agents are first-class identity + state; topology and skill access are policy layered on top.

## See also

- [Reference: agent CLI](../reference/cli/agent.md)
- [Reference: profile-yaml](../reference/dsl/profile-yaml.md)
- [Reference: multi-agent config](../reference/config/multi-agent.md)
- [Concepts: topology](topology.md)
- [Concepts: memory](memory.md)
