---
type: concept
topic: architecture
audience: [human, agent]
---

# Sessions and the three-level model

Reyn separates *who is acting*, *which conversation*, and *the machinery that runs
a skill* into three distinct levels. Keeping them apart is what lets one agent hold
many parallel conversations, each independently persisted and rewindable, without
the conversations bleeding into each other.

## The three levels

| Level | What it is | Owns |
|---|---|---|
| **Agent** | the **identity / node** — a long-lived actor, addressable by name | name, memory, permissions, workspace scope, peer addressing |
| **Session** | a **conversation** under an Agent (one Agent → many Sessions) | message history, inbox / outbox, current task, transient state |
| **SkillRuntime** | the **per-skill OS-runtime host** | builds and owns the execution context for one skill run; drives the phase-graph through the OS |

The hierarchy:

```
AgentRegistry → Agent (identity) → N Sessions → SkillRuntime → OS runtime → Phase
```

This is the mainstream agent-platform shape, not a novel design. OpenAI's
Assistants API is the exact analogue — **Assistant** (identity) : **Thread**
(conversation) : **Run** (one execution) — and graph/thread-id/invocation in other
frameworks embodies the same split. Reyn's distinction is not the model but what
sits *beneath* it: every Session is event-sourced, permission-gated, and
time-travelable (see below).

## Multiple Sessions vs multiple Agents

The two axes are easy to confuse. The dividing line is **identity**:

- **Multiple Agents** ⇒ *different identity* — different role, permissions, memory,
  or trust boundary. "Different **who**." A team of specialists that delegate to or
  collaborate with each other.
- **Multiple Sessions** ⇒ *same identity, parallel conversations* — shared memory,
  permissions, and workspace. "Same **who**, different conversation." One actor
  juggling several tasks at once.

The working rule:

> Need a different role, permission, or knowledge boundary? → a new **Agent**.
> Same agent doing another thing in parallel? → a new **Session**.

Because **permissions belong to the identity** (the Agent boundary) and are shared
across that Agent's Sessions, a task that needs *different* permissions is by
definition a different identity — so it is a sub-agent, not a sub-session. That
keeps the criterion single and unambiguous. The same split recurses: forking a
Session (a sub-session) keeps the identity and branches the conversation;
delegating to a sub-agent changes the identity.

## What a Session owns

A Session is the unit of conversation. It holds the message history, the inbox and
outbox, the current task, and transient run state. Identity-scoped things — memory,
permissions, workspace scope, peer addressing — live on the Agent and are shared by
all of that Agent's Sessions; conversation-scoped things stay isolated per Session.

Persistence and recovery key on the **Session**:

- **Per-session persistence** — each Session's state is snapshotted independently,
  so the conversations under one Agent are saved and restored separately.
- **Per-session time-travel** — rewind / fork apply to a single Session; a
  sub-session fork *is* a time-travel branch of its parent. See
  [time-travel](../runtime/time-travel.md).
- **Crash recovery** — on restart the full multi-session structure is reconstructed
  from the event log and snapshots, not just a single conversation.

## Transports route to Sessions

A *transport* is any way a message reaches an Agent — the interactive REPL, the web
UI, a scheduled cron job, a peer agent, and so on. A transport does not own a
conversation; it **routes** an incoming message to a Session by a **routing-key**:

- **Default — deterministic mapping.** A transport's native conversation id maps to
  a Session, namespaced by transport (a web tab, a cron job name, a chat thread).
  The first message auto-creates the Session; the same id resumes it. This gives
  stateful, isolated, per-conversation routing with zero configuration. Today the
  web UI routes per browser tab and cron routes per scheduled job.
- **Explicit — join an existing Session.** A message can target an existing Session
  by id, which is how one transport bridges into a conversation another started. A
  target that does not exist is an error: Sessions are created only by the mapping
  default or an explicit spawn, never silently by a mistyped id.

Routing is scoped **within one Agent** — joining is across that Agent's own
Sessions (shared identity, safe). Reaching a *different* Agent is delegation, not a
join. Additional transports route through the same model as they are added.

## See also

- [Multi-agent](multi-agent.md) — the four compositional surfaces for *different*
  identities (delegation, topology, A2A, MCP-serve).
- [Time-travel](../runtime/time-travel.md) — the rewind / fork / crash-recovery
  machinery that operates per Session.
- [Architecture](../architecture/architecture.md) — where the Agent / Session /
  runtime split sits in the component layers.
