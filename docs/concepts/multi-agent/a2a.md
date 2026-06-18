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

| Method / capability | Status | Notes |
|---|---|---|
| `message/send` (synchronous reply) | ✅ | Default mode — peer waits for the final reply text. |
| `message/send` (async via `async_mode: true`) | ✅ | Returns an A2A `Task` envelope; peer polls or subscribes. See [Task lifecycle](#task-lifecycle-and-async-execution-fp-0001). |
| `GET /a2a/tasks/{run_id}` (status polling) | ✅ | Reports `running` / `input-required` / `completed` / `failed` / `cancelled`. |
| `POST /a2a/tasks/{run_id}/cancel` | ✅ | Cancels the underlying `asyncio.Task` (idempotent). |
| `GET /a2a/tasks/{run_id}/events` (SSE stream) | ✅ | Reyn-native streaming surface; closes on terminal status. |
| Mid-run `ask_user` injection | ✅ | Task transitions to `input-required`; reply with `message/send` + `task_id`. |
| Push notifications (`params.webhook_url`) | ✅ | Reyn POSTs JSON payloads on each status transition. |
| Agent Card discovery (`.well-known/agent-card.json`) | ✅ | Per-agent + multi-agent index endpoints. |
| Multi-turn history persistence | ✅ | Same backing as MCP; per-agent `Session.history`. |
| `message/stream` (standalone JSON-RPC method) | ❌ | Use the `/events` SSE endpoint above instead. |
| Authentication (bearer tokens / OAuth) | ❌ | Out of scope for v1; relies on network-level access control. |
| Non-text message parts (`file`, `data`) | ❌ | Files exchanged via the Reyn workspace today. |

`message/send` is the headline of the MVP because it covers the most
common interop pattern: peer agent has a question for a Reyn agent,
gets the final reply text. Multi-turn history is preserved across
calls because Reyn's `Session.history` is per-agent and
persistent — exactly the same property the MCP path relies on. The
async task lifecycle layered on top (FP-0001, detailed below) lets
peers drive long-running skills, react to mid-run `ask_user`, and
cancel without changing the wire shape of the `message/send` call.

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

## Task lifecycle and async execution (FP-0001)

A2A peers can now interact with skills that emit `ask_user` mid-execution.
Earlier versions could only run synchronously — `message/send` returned
either a finished reply or a timeout placeholder, with no path to inject
a mid-run answer.

### Async mode

Submit a request with `params.async_mode: true` (or with a `params.webhook_url`
set) to spawn a background task instead of waiting synchronously:

```json
{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {
    "message": {"parts": [{"kind": "text", "text": "review the PR"}]},
    "async_mode": true
  }
}
```

Response (= A2A Task envelope):

```json
{
  "jsonrpc": "2.0", "id": 1,
  "result": {"kind": "task", "id": "<run_id>", "status": "running", "agent_name": "..."}
}
```

### Polling

`GET /a2a/tasks/{run_id}` returns the current state:

```json
{"run_id": "...", "status": "running" | "input-required" | "completed" | "failed" | "cancelled",
 "question": "...", "result": "...", "error": "..."}
```

### Mid-run ask_user

When the running skill fires `ask_user`, the task transitions to
`input-required` and the prompt text is exposed as `question`. To answer:

```json
{
  "jsonrpc": "2.0", "id": 2, "method": "message/send",
  "params": {
    "task_id": "<run_id>",
    "message": {"parts": [{"kind": "text", "text": "yes proceed"}]}
  }
}
```

The skill resumes; subsequent polls show `status: "running"` again, or
the next `input-required`, until terminal.

### SSE streaming

`GET /a2a/tasks/{run_id}/events` returns a `text/event-stream` of the
task's emitted events. Closes when the task reaches a terminal status.

### Push notifications

If `params.webhook_url` is set on the initial `message/send`, Reyn POSTs
JSON payloads to the URL on each status transition (`running` →
`input-required` → `running` → `completed`/`failed`/`cancelled`).
Errors talking to the webhook are logged, not raised — the task
progresses regardless.

### Cancellation

`POST /a2a/tasks/{run_id}/cancel` cancels the underlying asyncio.Task.
Idempotent for tasks already in terminal status.

### Agent Card capabilities

The Agent Card now advertises:

```json
{"capabilities": {"streaming": true, "pushNotifications": true, "stateTransitionHistory": false}}
```

## See also

- [MCP integration](../tools-integrations/mcp.md) — the symmetric case
- [Multi-agent](../multi-agent/multi-agent.md) — Reyn-internal agent topologies
