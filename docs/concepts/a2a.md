---
type: concept
topic: integration
audience: [human, agent]
---

# A2A (Agent2Agent Protocol)

Reyn exposes each registered agent as an A2A-addressable peer, so other
agent frameworks (LangGraph, CrewAI, custom A2A speakers, …) can
discover and talk to Reyn agents through a standard wire protocol.

## What is A2A

A2A is a peer-to-peer protocol for autonomous agents, originally
proposed by Google. Each agent publishes an **Agent Card** at a
well-known URL describing its identity, capabilities, and JSON-RPC
endpoint; peers fetch the card to learn how to talk, then send
messages over JSON-RPC 2.0. Spec: <https://google.github.io/A2A/>.

The complementary contrast with MCP:

| Protocol | Reyn's role | Peer's role |
|---|---|---|
| **MCP** | Tool provider — exposes `list_agents` / `send_to_agent` | Outer LLM client treating Reyn as a tool source |
| **A2A** | Addressable peer — each agent has its own endpoint | Another autonomous agent, conversing with Reyn agents directly |

Both run on the same Reyn web gateway (`reyn web`) and share the same
backing implementation (the registry, budget, permissions, history),
so you don't have to choose — Reyn is reachable via both protocols
simultaneously.

## How Reyn exposes agents

When `reyn web` is running, every registered Reyn agent (= every
directory under `.reyn/agents/`) is automatically published at:

```
GET  /a2a/agents/<name>/.well-known/agent-card.json
POST /a2a/agents/<name>                            ← JSON-RPC endpoint
GET  /a2a/agents                                   ← list-all helper
```

The Agent Card surfaces:

- the agent's name (= addressable identity)
- the agent's `role` text (from `profile.yaml`) as `description`
- `capabilities` — what's supported on the wire (streaming, push
  notifications, task lifecycle)
- `defaultInputModes` / `defaultOutputModes` — currently `text/plain`
- `skills` — a single coarse-grained `chat` capability. Reyn's
  internal skill catalogue stays opaque to the A2A peer (P7); the
  agent decides internally which Reyn skill to invoke for each
  incoming message.

## What's supported

| Method | v1 | v2 (planned) |
|---|---|---|
| `message/send` | ✅ synchronous reply | — |
| `message/stream` | ❌ | streaming SSE responses |
| `tasks/get` / `tasks/cancel` | ❌ | task lifecycle for long-running runs |
| Push notifications | ❌ | callback-style results |
| Authentication | ❌ | bearer tokens / OAuth |
| Non-text parts (`file`, `data`) | ❌ | file uploads via Reyn workspace |

`message/send` is the headline of the MVP because it covers the most
common interop pattern: peer agent has a question for a Reyn agent,
gets the final reply text. Multi-turn history is preserved across
calls because Reyn's `ChatSession.history` is per-agent and
persistent — exactly the same property the MCP path relies on.

## Why both MCP and A2A

MCP and A2A solve different problems even though both involve "an
outer LLM talking to Reyn":

- **MCP** is built around tool calling. The outer LLM's runtime
  decides when to invoke `send_to_agent`; it's a synchronous tool
  call from the LLM's point of view.
- **A2A** is built around peer addressing. The outer agent treats a
  Reyn agent as another autonomous entity, with its own card,
  capabilities, and conversational state. The peer doesn't model
  Reyn as a "tool" — it models Reyn as a "colleague".

For Reyn this is mostly a wire-format choice; the underlying engine
is the same. Pick MCP when the outer system is an LLM with tool
calling. Pick A2A when the outer system is itself an agent.

## See also

- [MCP integration](mcp.md) — the symmetric case
- [Multi-agent](multi-agent.md) — Reyn-internal agent topologies
