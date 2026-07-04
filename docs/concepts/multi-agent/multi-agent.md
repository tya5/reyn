---
type: concept
topic: architecture
audience: [human, agent]
---

# Multi-agent

A reyn process can host any number of long-lived **agents** — each an *identity* with its own profile, memory layer, permissions, and workflow catalogue view. Each agent runs one or more **Sessions**: independent conversations under that identity, each with its own history, inbox, and current task (see [Sessions](sessions.md) for the Agent / Session / SkillRuntime three-level model). Agents talk to humans (one at a time, via attach) and to each other (through a structured request-response channel).

## Two layers of multi-agent in Reyn

Reyn does not have a single multi-agent feature. It has two distinct compositional surfaces for agent-to-agent interaction, each suited to a different scope. The differentiating claim: **both layers preserve the same OS invariants** — P4 (constrained candidate set), P6 (events for every transition), and the permission system.

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 4:  reyn mcp serve                                        │
│            (external MCP clients call INTO Reyn agents)          │
│              ↑ list_agents()  ↑ send_to_agent(name, msg)         │
├──────────────────────────────────────────────────────────────────┤
│  Layer 3:  delegate_to_agent                                     │
│            (agent → agent, in-process, chain_id correlated)      │
└──────────────────────────────────────────────────────────────────┘
                Both layers enforce: P4 + P6 + permissions
```

### Layer summary

| Layer | Mechanism | Wiring | Boundary | Typical use | Reference |
|-------|-----------|--------|----------|-------------|-----------|
| 3 | `delegate_to_agent` | runtime + topology | same-process | specialist hand-off ("research agent → writer agent") | [../multi-agent/topology.md](../multi-agent/topology.md) |
| 4 | `reyn mcp serve` | runtime | external client | exposing agent fleet to Claude Code, Cursor, or any MCP-aware client | [../tools-integrations/mcp.md](../tools-integrations/mcp.md) |

### What stays the same across both layers

- **P4 — constrained candidate set.** At every layer the LLM picks from an OS-curated set: agents reachable via topology, or tools the MCP server exposes. No layer lets the LLM invent agents not already in the catalogue.
- **P6 — events for every transition.** Every layer emits structured events on entry, completion, and failure. Cross-layer chains are reconstructable by `grep <chain_id>` across each agent's `events.jsonl`. The event log is the single audit channel.
- **Permission gating.** File, MCP, shell, and web permissions are checked at the OS level regardless of which layer triggered the call.

### When to pick which layer

- "Different specialist roles, each talking to each other" → **Layer 3** (`delegate_to_agent`)
- "Outside MCP-aware tools (Claude Code, Cursor, OpenAI Agents SDK, etc.) need to call my agents" → **Layer 4** (`reyn mcp serve`)

## What is an agent?

An agent is a directory at `.reyn/agents/<name>/` (its persistent identity) plus one or more in-memory **Sessions** the runtime spins up on demand:

- `profile.yaml` — name, role (system-prompt persona), `allowed_mcp` (optional)
- `history.jsonl` — append-only conversation log
- `events.jsonl` — runtime audit log
- `memory/` — agent-scoped memory layer (the shared layer at `.reyn/memory/` is visible to every agent)
- `runs/` — per-skill-spawn workspace

The `default` agent is auto-created when needed; named agents come from `reyn agent new`.

## AgentRegistry

A single `AgentRegistry` instance per process owns all loaded agents and the Sessions under each — internally a `name → {sid → Session}` map with a shared `Agent` identity per name. It handles:

- **Lazy load** — agents are instantiated on first attach or first inter-agent message, not at startup.
- **Attach pointer** — exactly one agent is the REPL-attached one at a time. Detached agents keep running their inbox loop (background skill progress, intervention queues), but their transient outbox messages are dropped — only durable history persists.
- **Outbox forwarder** — a per-agent task pumps the attached agent's outbox into a shared REPL queue.
- **Topology gate** — `permit(from, to)` consults declared topologies before allowing inter-agent sends. See [../multi-agent/topology.md](../multi-agent/topology.md).

## Attach model

`reyn chat researcher` makes `researcher` the attached agent. While attached, `/attach default` switches the pointer back; `researcher` keeps its inbox loop running. If a delegation chain is mid-flight when you switch, you'll come back to find the resolution sitting in the outbox.

## Agent-to-agent messaging

When a router decision emits `messages_to_agents: [{to, request}, ...]`, the Session routes each entry to the target's inbox as an `agent_request` payload:

```
{from_agent, request, depth, chain_id}
```

The receiving agent's `session.run()` consumes it, runs its own router, and either replies immediately (`agent_response` back to the sender) or **defers** if it wants to delegate further.

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

`messages_to_agents` may contain multiple entries. The pending chain's `waiting_on` set holds all of them; the synthesized reply happens only after **every** delegate responds (wait-for-all). A single slow delegate delays the whole synthesis until either it responds or `safety.timeout.chain_seconds` (default 60s) elapses — at which point a `chain_timeout` event fires and a synthesized error response unblocks the upstream agent.

## User-initiated vs agent-initiated chains

The deferred-reply mechanic applies only to chains where another agent is waiting upstream. For **user-initiated** chains, the originating agent ships its router's `reply_text` to the user immediately (interim acknowledgement), then a second pass after delegate responses produces the final answer. Two visible messages, never one synthesized lump.

That preserves the existing chat UX ("you'll see I'm working on it") while letting agent-to-agent chains compose cleanly into one reply per request.

## max_hop_depth

`safety.loop.max_agent_hops` (default 3) caps how far a chain can extend. `depth = 0` is the user input; each `_send_to_agent` increments. A send with `depth > max_agent_hops` is refused with an `agent_message_refused` event. See [reference: multi-agent config](../../reference/config/multi-agent.md).

## What the OS does NOT manage

- **Topology**: who can send to whom is a separate concept (see [../multi-agent/topology.md](../multi-agent/topology.md)) consulted by the registry's `permit()`.
- **Memory layering**: shared vs agent layer is read/written by the router's classify phase; the registry doesn't touch memory files.

Agents are first-class identity + state; topology and workflow access are policy layered on top.

## Agent ID propagation (FP-0016 Component E)

Enterprise deployments need per-agent attribution: SOC2 / ISO27001 / METI v1.1 audit requirements mandate proving "which agent did what" at the actor level — not at the human user level. Reyn assigns every running instance an `agent.id` (configured via `reyn.yaml`; defaults to `reyn/<hostname>`) and propagates it through three channels:

1. **P6 events**: every event emitted from the session carries `agent_id` in its payload. This makes the event log replay-capable as an audit trail of agent-attributed actions.
2. **MCP HTTP calls**: outgoing requests to HTTP-mode MCP servers add an `X-Reyn-Agent-Id: <agent.id>` header. Downstream MCP servers can apply RBAC based on the calling agent identity (= the "Entra Agent ID" pattern from Microsoft's identity model).

Configuration:

```yaml
# reyn.yaml
agent:
  id: "reyn/acme-corp/code-review-agent"
```

Sane default: when `agent.id` is omitted, Reyn uses `reyn/<hostname>` so the audit trail is never empty.

Recommended format: `reyn/<org>/<role>` (= operator-defined; Reyn does not enforce structure beyond requiring a non-empty string).

Cross-references:
- [`docs/reference/config/reyn-yaml.md`](../../reference/config/reyn-yaml.md) — `agent:` block field reference
- [`docs/reference/runtime/events.md`](../../reference/runtime/events.md) — `agent_id` base event field
- [`docs/concepts/runtime/secret-handling.md`](../runtime/secret-handling.md) — credential scoping + OAuth lifecycle (= the other half of FP-0016)

## See also

- [Concepts: Sessions](sessions.md) — the Agent / Session / SkillRuntime three-level model (one identity, many conversations)
- [Reference: agent CLI](../../reference/cli/agent.md)
- [Reference: multi-agent config](../../reference/config/multi-agent.md)
- [Concepts: topology](../multi-agent/topology.md)
- [Concepts: memory](../data-retrieval/memory.md)
