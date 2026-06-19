---
type: concept
topic: architecture
audience: [human, agent]
---

# Agent interaction layers

Three structurally distinct things can set an agent in motion: an **outside
system calls in**, **Reyn itself raises a turn**, or **something intervenes
inside a turn already running**. Naming these as three layers keeps the agent's
control plane explicit and governable — which is the point: autonomy-first
frameworks (the OpenClaw / Hermes class of self-hosted agents) leave "how the
world reaches the agent" largely to free wiring and LLM discretion; Reyn makes
the trigger boundary a first-class, auditable surface, consistent with its
predictability-over-autonomy stance.

All three layers ultimately converge on the same primitive — a message placed on
an **agent's inbox** (the `send_to_agent_impl` path). They differ only in *who
initiates* and *when*.

```
┌─────────────────────────────────────────────────────────────┐
│  1. External connection  (outside → Reyn; Reyn is a server)  │
│       MCP server · A2A server · gateway                       │
├─────────────────────────────────────────────────────────────┤
│  2. Internal trigger     (Reyn → a fresh agent turn)         │
│       cron · inject_message (proposed)                        │
├─────────────────────────────────────────────────────────────┤
│  3. In-turn intervention (interrupt a turn in flight)        │
│       lifecycle hooks (proposed)                              │
└─────────────────────────────────────────────────────────────┘
                          ↓ all converge
                   agent inbox / send_to_agent_impl
```

> **Status note.** Layer 1 and cron (layer 2) are **implemented**.
> `inject_message` (layer 2) and the entire hook layer (layer 3) are **proposed**
> — design-stage, not in the codebase. This page marks each accordingly; do not
> read a proposed mechanism as current behavior.

## 1. External connection layer (outside calls Reyn)

An external caller delivers a message to an agent session; Reyn acts as a passive
server. Three connection kinds, all **implemented**:

| Connection | Caller | Reply delivery |
|---|---|---|
| **MCP server** (SSE / stdio) — `src/reyn/mcp/server.py` | AI clients (Claude Code, Cursor) | Synchronous — the caller blocks for the reply |
| **A2A server** (HTTP JSON-RPC) — `src/reyn/interfaces/web/routers/a2a.py` | Peer AI agents (LangGraph, CrewAI, …) | Synchronous, or async to a caller-supplied `webhook_url` |
| **Gateway** (Slack / LINE / …) — `src/reyn/plugins/` | Humans, via a chat platform | Asynchronous — Reyn must call the platform API to deliver |

**The outbound asymmetry.** MCP and A2A can **delegate the outbound reply to the
caller**: the response either returns synchronously or is POSTed to a callback URL
the caller provided, so Reyn needs no platform-specific outbound code. The
**gateway is different — Reyn owns the outbound path**: there is no caller waiting
on a socket, so Reyn must actively push the reply back out to the platform.

In the current code the gateway delivers **inbound only** (the `sample_line` /
`sample_slack` webhooks call `push_to_agent`); outbound replies are expected to go
through a separate MCP tool (e.g. a Slack MCP server) rather than the gateway
itself. Making a gateway own both inbound *and* outbound — register its own
outbound MCP tool so a self-contained gateway handles send and receive — is a
**proposed** completion of this layer. (A **proposed** rename of the `plugins/`
package to `gateway/` reflects this bidirectional-bridge role; the package is
`plugins/` today.)

See also: [A2A](../multi-agent/a2a.md), [MCP](../tools-integrations/mcp.md).

## 2. Internal trigger layer (Reyn raises a turn)

Here Reyn itself starts a fresh agent turn — no external caller involved.

- **cron** — `src/reyn/runtime/cron/` (**implemented**). A scheduled `CronJob`
  dispatches a message to a target agent's inbox, producing an attributed agent
  turn from a scheduled trigger.
- **`inject_message`** (**proposed**) — a programmatic call that places a message
  on an agent's inbox to raise a turn.

The two are **structurally the same operation** — *put a message on an agent's
inbox to start a turn* — and differ only in what causes the dispatch (a schedule
vs. an explicit call). That equivalence is why they belong in one layer.

## 3. In-turn intervention layer (interrupt a turn already running) — proposed

Unlike the first two layers, which *start* a turn, this layer **interrupts or
augments a turn already in flight**. It is entirely **proposed** — Reyn has no
hook mechanism today (there are no lifecycle callback points in the router loop
or session).

The proposed shape is a set of lifecycle **hooks**, dispatched from
`src/reyn/core/dispatch/dispatcher.py`:

- `pre_tool_call` — before a tool runs (able to block or rewrite).
- `post_tool_call` / `transform_tool_result` — after a tool runs.
- `pre_llm_call` — before the LLM call (e.g. context injection).
- `transform_llm_output`, plus session-lifecycle events.

This is the layer that maps most directly to the hook systems in the surveyed
competitors; in Reyn it would sit under the same permission and event discipline
as the rest of the OS.

## Implemented vs. proposed (summary)

| Layer | Implemented | Proposed |
|---|---|---|
| 1 — External connection | MCP, A2A, gateway (inbound) | gateway outbound completion; `plugins/`→`gateway/` rename |
| 2 — Internal trigger | cron | `inject_message` |
| 3 — In-turn intervention | — | lifecycle hooks (whole layer) |

## Why three layers

The value is a single, exhaustive question for any new way of reaching an agent:
*is this outside→Reyn, Reyn→new-turn, or within-turn?* Every interaction lands in
exactly one layer and converges on the same inbox primitive, so the control plane
stays auditable and each new mechanism inherits the OS's permission and event
guarantees rather than being bolted on. It is the governance-first counterpart to
the autonomy-first frameworks where these paths are ad hoc.

## See also

- [LLM invocation surfaces](llm-invocation-surfaces.md) — router vs. phase (a different axis: how the LLM is *called*, not how the agent is *reached*)
- [Multi-agent](../multi-agent/multi-agent.md) · [A2A](../multi-agent/a2a.md) · [MCP](../tools-integrations/mcp.md)
- [Principles](principles.md) — P4 (constrained decision engine), the governance stance behind this model
