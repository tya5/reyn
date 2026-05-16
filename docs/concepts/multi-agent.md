---
type: concept
topic: architecture
audience: [human, agent]
---

# Multi-agent

A reyn process can host any number of long-lived **agents**, each one a ChatSession with its own profile, history, memory layer, inbox, and skill catalogue view. Agents talk to humans (one at a time, via attach) and to each other (through a structured request-response channel).

## Four layers of multi-agent in Reyn

Reyn does not have a single multi-agent feature. It has four distinct compositional surfaces, each suited to a different scope and wiring time. The differentiating claim: **all four layers preserve the same OS invariants** — [P4](principles.md#p4-llm-is-a-constrained-decision-engine) (constrained candidate set), [P6](principles.md#p6-events-are-the-audit-truth) (events for every transition), and the permission system. Many frameworks have one or two of these surfaces; Reyn's distinction is uniform invariants across all four.

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 4:  reyn mcp serve                                        │
│            (external MCP clients call INTO Reyn agents)          │
│              ↑ list_agents()  ↑ send_to_agent(name, msg)         │
├──────────────────────────────────────────────────────────────────┤
│  Layer 3:  delegate_to_agent                                     │
│            (agent → agent, in-process, chain_id correlated)      │
├──────────────────────────────────────────────────────────────────┤
│  Layer 2:  run_skill  Control IR op                              │
│            (phase invokes a sub-skill at runtime, LLM-chosen)    │
├──────────────────────────────────────────────────────────────────┤
│  Layer 1:  @sub_skill  graph node                                │
│            (skill graph statically embeds another skill)         │
└──────────────────────────────────────────────────────────────────┘
                All layers enforce: P4 + P6 + permissions
```

### Layer summary

| Layer | Mechanism | Wiring | Boundary | Typical use | Reference |
|-------|-----------|--------|----------|-------------|-----------|
| 1 | `@sub_skill` graph node | compile-time | same-process | static composition ("phase A always calls skill X") | [graph.md](../reference/dsl/graph.md) |
| 2 | `run_skill` Control IR op | LLM-runtime | same-process | dynamic sub-skill choice ("phase decides which sub-skill") | [control-ir.md](../reference/runtime/control-ir.md#run_skill) |
| 3 | `delegate_to_agent` | runtime + topology | same-process | specialist hand-off ("research agent → writer agent") | [topology.md](topology.md) |
| 4 | `reyn mcp serve` | runtime | external client | exposing agent fleet to Claude Code, Cursor, or any MCP-aware client | [mcp.md](mcp.md) |

> **FP-0034 Phase 6 (2026-05-16) routing note**: Layer 3
> `delegate_to_agent` and Layer 2 `run_skill` keep their handler names
> for the diagrams and Control IR. The LLM-visible surface is the
> universal `invoke_action(action_name="agent.peer__<name>", args={...})`
> for delegations and `invoke_action(action_name="skill__<name>",
> args={...})` for skill invocations — `universal_dispatch.py` routes
> these to the same handlers. Permissions, events, and chain semantics
> are unchanged.

### What stays the same across all four layers

- **P4 — constrained candidate set.** At every layer the LLM picks from an OS-curated set: skills it owns, agents reachable via topology, or tools the MCP server exposes. No layer lets the LLM invent agents or skills not already in the catalogue.
- **P6 — events for every transition.** Every layer emits structured events on entry, completion, and failure. Cross-layer chains are reconstructable by `grep <chain_id>` across each agent's `events.jsonl`. The event log is the single audit channel.
- **Permission gating.** File, MCP, shell, and web permissions are checked at the OS level regardless of which layer triggered the call. A Layer 3 delegated call does not bypass permission rules, and a Layer 2 sub-skill must declare its own permissions.
- **Workspace isolation.** Each layer respects skill-scoped workspace boundaries. A sub-skill invoked via Layer 1 or 2 reads only the inputs it declares.

### When to pick which layer

- "I always need step Y inside skill X" → **Layer 1** (`@sub_skill` graph node)
- "Skill X needs to call one of N sub-skills depending on input" → **Layer 2** (`run_skill` Control IR op)
- "Different specialist roles, each with their own skill catalogue, talking to each other" → **Layer 3** (`delegate_to_agent`)
- "Outside MCP-aware tools (Claude Code, Cursor, OpenAI Agents SDK, etc.) need to call my agents" → **Layer 4** (`reyn mcp serve`)

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

`reyn chat researcher` makes `researcher` the attached agent. While attached, `/attach default` switches the pointer back; `researcher` keeps its inbox loop running. If a delegation chain is mid-flight when you switch, you'll come back to find the resolution sitting in the outbox.

## Agent-to-agent messaging

When a router decision emits `messages_to_agents: [{to, request}, ...]`, ChatSession routes each entry to the target's inbox as an `agent_request` payload:

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

`safety.loop.max_agent_hops` (default 3) caps how far a chain can extend. `depth = 0` is the user input; each `_send_to_agent` increments. A send with `depth > max_agent_hops` is refused with an `agent_message_refused` event. See [reference: multi-agent config](../reference/config/multi-agent.md).

## What the OS does NOT manage

- **Topology**: who can send to whom is a separate concept (see [topology.md](topology.md)) consulted by the registry's `permit()`.
- **Skill access**: the LLM-side skill filter is per-agent via `profile.allowed_skills`; the OS just respects what the profile says.
- **Memory layering**: shared vs agent layer is read/written by the router's classify phase; the registry doesn't touch memory files.

Agents are first-class identity + state; topology and skill access are policy layered on top.

## Agent ID propagation (FP-0016 Component E)

Enterprise deployments need per-agent attribution: SOC2 / ISO27001 / METI v1.1 audit requirements mandate proving "which agent did what" at the actor level — not at the human user level. Reyn assigns every running instance an `agent.id` (configured via `reyn.yaml`; defaults to `reyn/<hostname>`) and propagates it through three channels:

1. **P6 events**: every event emitted from the session carries `agent_id` in its payload. This makes the event log replay-capable as an audit trail of agent-attributed actions.
2. **MCP HTTP calls**: outgoing requests to HTTP-mode MCP servers add an `X-Reyn-Agent-Id: <agent.id>` header. Downstream MCP servers can apply RBAC based on the calling agent identity (= the "Entra Agent ID" pattern from Microsoft's identity model).
3. **Sub-skill calls**: nested `run_skill` invocations inherit the parent's `agent_id` (= the same identity persists through the entire call tree from chat entry to deepest sub-skill).

Configuration:

```yaml
# reyn.yaml
agent:
  id: "reyn/acme-corp/code-review-agent"
```

Sane default: when `agent.id` is omitted, Reyn uses `reyn/<hostname>` so the audit trail is never empty.

Recommended format: `reyn/<org>/<role>` (= operator-defined; Reyn does not enforce structure beyond requiring a non-empty string).

Cross-references:
- [`docs/reference/config/reyn-yaml.md`](../reference/config/reyn-yaml.md) — `agent:` block field reference
- [`docs/reference/runtime/events.md`](../reference/runtime/events.md) — `agent_id` base event field
- [`docs/concepts/secret-handling.md`](secret-handling.md) — credential scoping + OAuth lifecycle (= the other half of FP-0016)

## See also

- [Reference: agent CLI](../reference/cli/agent.md)
- [Reference: profile-yaml](../reference/dsl/profile-yaml.md)
- [Reference: multi-agent config](../reference/config/multi-agent.md)
- [Concepts: topology](topology.md)
- [Concepts: memory](memory.md)
