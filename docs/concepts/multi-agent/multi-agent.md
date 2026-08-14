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
│  Layer 3:  run_prompt / send_to_session                          │
│            (agent → agent, in-process, chain_id correlated)      │
└──────────────────────────────────────────────────────────────────┘
                Both layers enforce: P4 + P6 + permissions
```

### Layer summary

| Layer | Mechanism | Wiring | Boundary | Typical use | Reference |
|-------|-----------|--------|----------|-------------|-----------|
| 3 | `run_prompt` / `send_to_session` | runtime + topology | same-process | specialist hand-off ("research agent → writer agent") | [../multi-agent/topology.md](../multi-agent/topology.md) |
| 4 | `reyn mcp serve` | runtime | external client | exposing agent fleet to Claude Code, Cursor, or any MCP-aware client | [../tools-integrations/mcp.md](../tools-integrations/mcp.md) |

### What stays the same across both layers

- **P4 — constrained candidate set.** At every layer the LLM picks from an OS-curated set: agents reachable via topology, or tools the MCP server exposes. No layer lets the LLM invent agents not already in the catalogue.
- **P6 — events for every transition.** Every layer emits structured events on entry, completion, and failure. Cross-layer chains are reconstructable by `grep <chain_id>` across each agent's `events.jsonl`. The event log is the single audit channel.
- **Permission gating.** File, MCP, shell, and web permissions are checked at the OS level regardless of which layer triggered the call.

### When to pick which layer

- "Different specialist roles, each talking to each other" → **Layer 3** (`run_prompt` / `send_to_session`)
- "Outside MCP-aware tools (Claude Code, Cursor, OpenAI Agents SDK, etc.) need to call my agents" → **Layer 4** (`reyn mcp serve`)

## What is an agent?

An agent is a directory at `.reyn/agents/<name>/` (its persistent identity) plus one or more in-memory **Sessions** the runtime spins up on demand:

- `profile.yaml` — name, role (system-prompt persona), `allowed_mcp` (optional),
  `preferences` (optional, #4206 ③ — free-override config, e.g. `output_language`;
  see [agent.md § `preferences`](../../reference/cli/agent.md#preferences-4206-slice-1-the-3-axis-free-override-not-restrict-only))
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

`run_prompt(collect="async")` (proposal 0067 P4e, #3978) addresses a LIVE peer `(target_agent, target_session)` and returns a `task_id` immediately — the reply arrives later via the `task_settled` hook point, not in this call. `task_id` ≡ `chain_id`; no separate id is minted.

`session_api.run_prompt_async` registers a `ChainManager` chain directly, once per call:

- `waiting_on={target_agent}` — always exactly one member. `run_prompt(async)` is a 1:1 call, not a join: two calls in one turn register two independent chains, never a shared `waiting_on` that grows past one.
- `kind="prompt"`, `requester` (the calling `(agent, session_id)`), `cancel` (wraps the target session's `cancel_inflight()`, fire-and-forget — without it a registered handle's `cancel` would be `None` and `cancel_task` would correctly, but permanently, report "cannot cancel").

The request reaches the target's inbox as an `agent_request` payload (`{from_agent, request, depth, chain_id}`, `depth=1` always) via `InterAgentMessaging.send_to_agent` — reused purely as delivery transport here, not through the older per-turn accumulation path described below.

### Settling the reply

When the target's reply lands, `InterAgentMessaging.handle_agent_response` checks the registered chain's `kind`: a non-`None` kind (as `run_prompt(async)` always sets) routes to `ChainManager.settle()`, not the older relay-continue path. `settle()`'s `deliver` disposition appends the reply to the caller's history and runs exactly one router turn so the LLM can act on it; `task_settled` fires on the *fact* of settling, independent of the disposition's outcome (ADR-0040 D4④) — a future `on_settle="drop"` caller would still get the hook fired, just with nothing delivered.

### What this replaces — permanently retired, not renamed

Before proposal 0067, a router decision emitting `messages_to_agents: [{to, request}, ...]` could defer its own reply if it wanted to delegate further: a `_PendingChain` accumulated `waiting_on` across multiple recipients, and when the set emptied, the agent re-ran its router with every delegate's response in history to synthesize ONE reply upstream — a "manager → delegate → synthesize" model, bounded by `max_hop_depth`, with the chain staying pending across further delegation rounds if the synthesis pass itself delegated again.

This shape — a chain whose `waiting_on` can hold more than one member (a **join**) — does not exist today and will not return: `delegate_to_agent`, its sole producer, retired in proposal 0067 P6, and architect's ruling (#3978, 2026-08-10) fixes `|waiting_on| == 1` as a permanent structural invariant of a task-kind chain (`>= 2` was, and remains, outside the task vocabulary). `messages_to_agents` as a router-decision field no longer exists; `RouterCallerState.send_to_agent` (the field this old model's handler read) has since been removed outright (#4144) rather than left as dead residue. The `ChainManager` substrate (register/settle/WAL shape/timeout arming) this old model used is what's shared with the still-live `run_prompt(async)` producer above — `InterAgentMessaging.send_to_agent` (a different, unrelated name from the removed field) is that substrate's delivery transport, not a leftover of the retired model.

Two more pieces of the retired model, for the same reason:

- **Fan-out.** `messages_to_agents` could name multiple entries; `waiting_on` held all of them, and the synthesized reply waited for **every** delegate (wait-for-all) or `safety.timeout.chain_seconds` — a direct consequence of the join shape above, retired with it.
- **User-initiated vs. agent-initiated UX.** The deferred-reply mechanic distinguished a chain with another agent waiting upstream (synthesized reply, one message) from a user-initiated one (interim ack immediately, final answer in a second pass — two visible messages). `run_prompt(async)`'s own shape is simpler: the tool call itself returns immediately (the interim ack), and `task_settled` delivers the one reply later — there is no second "waiting agent" case to distinguish from, because there is no longer a chain that stays pending across further delegation rounds.

### Reply routing across delegating sessions

`Session` vs `Agent` are distinct (see [Sessions](../multi-agent/sessions.md)): a single Agent can run several Sessions in parallel. When a **non-main Session** on an Agent DELEGATES to a peer (not just spawns a sub-agent), the delegating Session's own `session_id` (`from_sid`) is threaded into the outgoing `submit_agent_request` call (`#2130`). This lets `_a2a_send_response` route the peer's reply back to `(from_agent, from_sid)` — the delegating Session specifically — instead of to the Agent's default `main` Session.

`from_sid="main"` (or absent) is the default/byte-identical path: `_a2a_send_response` treats an absent or `"main"` value as the unchanged main-Session case, so ordinary (main-Session-initiated) delegation is untouched by this threading.

This is in-process delegation only. A cross-process external peer that does not echo `from_sid` back degrades to `None` → `main` — a safe fallback (the reply reaches the Agent's main Session rather than being lost silently), not a routing failure.

A different case looks similar but resolves the opposite way: `from_sid` names a real, once-live Session that is simply **no longer loaded** (the spawner Session has since gone away). This is not the "peer never echoed a sid" case above — a sid was named — so the reply is **logged and dropped**, not routed to `main`. Falling back to `main` here would reintroduce the exact misroute `#2130` fixed: an orphaned reply silently landing in the wrong Session's history instead of visibly failing.

There is no dedicated reference doc for the internal `(agent, sid)` A2A routing scheme beyond this section and the code (`_a2a_send_response`) — this section is that scheme's documentation.

### chain_id

Every top-level user submission mints a `chain_id` (uuid4 hex) at `submit_user_text`. It propagates verbatim through:

- inbox payloads (every hop)
- history meta on every `_append_history` involved in the chain (sources: `agent_request`, `agent_request_outgoing`, `agent_response`, `agent_response_outgoing`)
- `agent_message_*` events

`chain_id` is **audit-only** — the router LLM does not see it, the CLI does not display it. To trace a chain end-to-end across agents, `grep <chain_id>` over each agent's `events.jsonl` and `history.jsonl`.

## max_hop_depth

`safety.loop.max_agent_hops` (default `3`) is a depth cap inherited from the retired multi-hop model above. `run_prompt(async)` always registers at `depth=1` and has no mechanism to increment it further, so any positive value behaves identically today; the setting's live effect is now a `run_prompt(async)`-delivery on/off switch (`0` refuses delivery, resolving the call as a timeout error rather than failing it outright), not a depth limit. See [reference: multi-agent config](../../reference/config/multi-agent.md) for the exact mechanics.

## What the OS does NOT manage

- **Topology**: who can send to whom is a separate concept (see [../multi-agent/topology.md](../multi-agent/topology.md)) consulted by the registry's `permit()`.
- **Memory layering**: shared vs agent layer is read/written by the router's classify phase; the registry doesn't touch memory files.

Agents are first-class identity + state; topology and workflow access are policy layered on top.

## Agent ID propagation (FP-0016 Component E)

Enterprise deployments need per-agent attribution: SOC2 / ISO27001 / METI v1.1 audit requirements mandate proving "which agent did what" at the actor level — not at the human user level. Reyn assigns every running instance an `agent_id` (configured via `reyn.yaml`; defaults to `reyn/<hostname>`) and propagates it through three channels:

1. **P6 events**: every event emitted from the session carries `agent_id` in its payload. This makes the event log replay-capable as an audit trail of agent-attributed actions.
2. **MCP HTTP calls**: outgoing requests to HTTP-mode MCP servers add an `X-Reyn-Agent-Id: <agent_id>` header. Downstream MCP servers can apply RBAC based on the calling agent identity (= the "Entra Agent ID" pattern from Microsoft's identity model).

Configuration:

```yaml
# reyn.yaml
agent_id: "reyn/acme-corp/code-review-agent"
```

Sane default: when `agent_id` is omitted, Reyn uses `reyn/<hostname>` so the audit trail is never empty.

Recommended format: `reyn/<org>/<role>` (= operator-defined; Reyn does not enforce structure beyond requiring a non-empty string).

Cross-references:
- [`docs/reference/config/reyn-yaml.md`](../../reference/config/reyn-yaml.md) — `agent_id` field reference
- [`docs/reference/runtime/events.md`](../../reference/runtime/events.md) — `agent_id` base event field
- [`docs/concepts/runtime/secret-handling.md`](../runtime/secret-handling.md) — credential scoping + OAuth lifecycle (= the other half of FP-0016)

## See also

- [Concepts: Sessions](sessions.md) — the Agent / Session / SkillRuntime three-level model (one identity, many conversations)
- [Reference: agent CLI](../../reference/cli/agent.md)
- [Reference: multi-agent config](../../reference/config/multi-agent.md)
- [Concepts: topology](../multi-agent/topology.md)
- [Concepts: memory](../data-retrieval/memory.md)
