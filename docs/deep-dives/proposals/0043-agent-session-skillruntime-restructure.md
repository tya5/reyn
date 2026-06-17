# FP-0043: Agent / Session / SkillRuntime restructure

**[lead-coder]** — owner-directed design (2026-06-17). Status: **proposed** (design;
SkillRuntime rename in progress, the rest staged & gated). FP number provisional —
docs-maintainer to confirm/renumber.

## Motivation — the naming layer-mismatch

`reyn.agent.Agent` is overloaded. Two distinct things both read as "agent":

- **`Agent` (agent.py)** is, in fact, a **per-skill OS-runtime host**: `run(skill)`
  builds an `OSRuntime` *for that skill* and runs the skill's phase-graph. It is
  not the multi-agent node, and it does not run an autonomous agent-loop — **the
  skill runs** (via `OSRuntime`); the Agent hosts the boundary (workspace /
  permissions / budget / events / secrets).
- **`ChatSession`** is the actual multi-agent **node** — `AgentRegistry` maps
  `agent_name → ChatSession` ("multiple agents = multiple ChatSession instances",
  PR10). It conflates the agent **identity** (name, `.reyn/agents/<name>/`
  persistence, permissions, workspace, peer-addressing) with the **conversation**
  (router-loop, inbox/outbox, message history, current task).

So "agent" names the OS-runtime core, while the real node is `ChatSession`, and
identity + conversation are fused. That mismatch is the 違和感.

## Target model — three levels

| Level | Concept | Owns |
|---|---|---|
| **Agent** | the **identity / node** (multi-agent first-class) | name, memory, permissions, workspace-scope, peer-addressing |
| **Session** | a **conversation / context** under an Agent (1 Agent : N) | message history, inbox/outbox, current task, transient state |
| **SkillRuntime** | the **per-skill OS-runtime host** (renamed from agent.py `Agent`) | builds + owns the per-skill execution context, runs the phase-graph via `OSRuntime` |

Hierarchy: `AgentRegistry → Agent (identity) → N Sessions → (per-skill) SkillRuntime → OSRuntime → Phase`.

This is the **industry-standard 3-level model** — OpenAI Assistants is the exact
analogue: **Assistant** (=Agent/identity) : **Thread** (=Session) : **Run**
(=SkillRuntime execution). LangGraph (graph : thread_id : invocation) and the
`agent_id`/`run_id` memory-scoping convention (mem0 etc.) embody the same split.
Not a novel/risky design — the mainstream shape.

## Usage criterion — multi-session vs multi-agent = **IDENTITY**

- **multiple Agents** ⇔ *different identity* (role / permissions / memory / trust
  boundary). "Different WHO." A team of specialists that delegate/collaborate.
- **multiple Sessions** ⇔ *same identity, parallel conversations* (shared memory /
  permissions / workspace). "Same WHO, different conversation." One actor juggling
  several tasks.

User-facing rule: *"Need a different role / permission / knowledge boundary? → new
**Agent**. Same agent doing another thing in parallel? → new **Session**."* The
criterion is recursive: **sub-session spawn (fork)** = same identity, branched;
**sub-agent delegate** = different identity. Edge case resolved cleanly:
**permissions are part of identity** (the Agent boundary), shared across Sessions;
if a task needs different permissions, that is a different identity = sub-agent —
keeping the criterion single.

## Session = conversation + inbox; interfaces are transports

Today: **N interfaces → 1 agent inbox → 1 conversation** (REPL `submit_user_text`,
web `agent.submit`, A2A `send_to_agent_impl`, peer `send_to_agent`, telegram via
broker — all push to the one ChatSession inbox).

Target: the **inbox moves per-agent → per-Session**. Interfaces are **transports**
that route an incoming message to a Session by a **routing-key**; the Agent
(identity) dispatches to the right Session's inbox and runs Sessions concurrently.
The same interface can target multiple Sessions; a peer A2A delegation can open /
target a Session.

> **OPEN — routing-key.** Candidates: (a) explicit `session_id` (interface
> specifies, OpenAI-Thread style), (b) per-source default Session, (c) per-interface.
> Lead lean: **(a) explicit session-id + per-source default** (interface-agnostic,
> flexible). To be settled.

## Inter-session interactions

Premise: a shared Agent identity. **Key axis = what is shared (agent-scoped) vs
isolated (session-scoped)** — identity (memory / permissions / peer) shared,
conversation (history / task) isolated; interactions are *controlled*
(notify / handoff / spawn), not implicit.

- **Shared state**: shared memory (A's discoveries visible to B), shared
  workspace/artifacts, shared budget pool (concurrent draw → coordination),
  shared/rolled-up event log.
- **Communication**: cross-post / notify (A's progress → B's inbox; the existing
  `forwarder` generalized), handoff (transfer a conversation).
- **Structure**: **sub-session spawn** (parent/child; see below), intra-agent
  delegation (A → B, the intra-agent analogue of `send_to_agent`).
- **Coordination**: resource locks (the existing `agent_locks` generalized),
  attention (attach/detach — the user focuses one Session, others run in
  background; exists today at the agent level).

Much of this **already exists at the agent level** (forwarder, `spawn_skill` /
`spawn_plan_task`, `agent_locks`, shared budget) — the restructure *generalizes*
these to inter-session, not inventing them.

## Sub-session spawn = a **live fork** (reuses time-travel)

A history-inheriting sub-session **is a live fork**. Reyn's time-travel
(ADR-0038 / #1533) fork already **branches from a turn-checkpoint, inheriting the
history up to that point, then diverges** (verified: the fork-edit forks from the
*predecessor turn checkpoint*). A sub-session = a **live** fork (vs the current
**inactive-branch** fork). Same machinery, different liveness; natural on the
event-sourced foundation (the inherited history = the event log up to the spawn
point).

Two modes the design exposes:

1. **Fork-inherit** — branch the parent's conversation up to the spawn point (the
   fork machinery).
2. **Task-spawn (fresh)** — today's `spawn_skill` / `spawn_plan_task`: an input,
   no conversation history (an independent unit of work).

> **OPEN — inheritance mode.** full-copy (complete context, heavy) / **compacted**
> (cheaper, via the compaction engine) / copy-on-write (shared-then-diverge).
> Default TBD.

## Persistence & differentiation

Sessions persist on Reyn's foundation: **event-sourced (P6, the audit truth,
replay-capable) + deterministic LLM-replay (the missing primitive, built-in) +
permission-gated mutation (state changes only via Control IR) + workspace/event
separation (P5 data vs P6 audit)**.

So Sessions are **replayable / forkable / permission-gated-auditable / self-hosted**.

- vs **OpenAI Threads** (opaque, server-side, no replay/control): decisively more open.
- vs **LangGraph** (checkpoint-snapshot + time-travel — *overlaps* on time-travel):
  the distinctive is the **combination** — event-source as the *OS truth* (not a
  snapshot bolt-on) + built-in deterministic LLM-replay + permission-gate + the
  data/audit separation. No single feature; the bundle.
- vs **Temporal** (durable event-sourced execution): closest, but Reyn is
  **agent-native** (events are phase / Control-IR / tool-dispatch semantics).

## Hard constraint — time-travel + crash-recovery must extend per-session

Today, time-travel (snapshot / journal / rewind / fork) and crash-recovery
(`restore_all`, the snapshot journal, WAL) **key on ChatSession** and operate at
the **ChatSession / snapshot-journal / event level** — they do **not** reference
the `Agent` class (so the SkillRuntime rename is orthogonal; verified by grep +
the named test gate). When the conversation/inbox move to Session, these must
become **per-session**:

- snapshot / journal per-Session;
- rewind / fork = "fork / rewind a Session" (and the sub-session-fork *is* this);
- crash-recovery (`restore_all`) restores the multi-session structure.

**Preserving time-travel + crash-recovery (extended to per-session) is a
first-class requirement.** Because the sub-session-fork unifies with the
time-travel fork, "don't break time-travel" and "enable sub-session inheritance"
are the *same* work. Flow-trace the snapshot/journal/rewind dependency before any
restructure step.

## Staged migration (each stage byte-gated; time-travel/crash-recovery preserved)

1. **SkillRuntime rename** (in progress) — `agent.py Agent` → `skill_runtime.SkillRuntime`,
   clean-rewrite-no-alias; **frees the `Agent` name** for the identity concept.
   (Surgically excludes the 280+ `Agent*` identity-layer identifiers —
   AgentRegistry/AgentProfile/AgentSnapshot/etc.)
2. **Extract Agent-identity** from ChatSession (name / memory / permissions /
   workspace / peer). The `AgentRegistry` / `AgentProfile` layer already partially
   holds this — consolidate.
3. **Session** = ChatSession's conversation part (inbox / history / task), N per Agent.
   (Possibly rename `ChatSession` → `Session`.)
4. **Per-session inbox + transport routing** (the routing-key).
5. **Per-session time-travel + crash-recovery** (snapshot/journal/rewind/fork/restore_all).
6. **Holistic chat-layer agent-naming pass** (the cosmetic `_build_agent` etc.
   deferred from stage 1).

## Open questions

- Routing-key (explicit session-id + per-source default — lean).
- Sub-session inheritance-mode default (full / compacted / copy-on-write).
- The exact Agent-identity ↔ Session boundary — esp. **memory and workspace
  scoping** (agent-scoped shared vs session-scoped). This is the lever that
  decides which inter-session interactions are possible.
- Migration sequencing & whether to rename `ChatSession` → `Session`.

## Competitive grounding (for reference)

OpenAI Assistants (Assistant / Thread / Run), LangGraph (graph / thread_id /
checkpoint + time-travel), mem0 (`agent_id` shared / `run_id` isolated memory
scoping), Cursor Agents Window / Claude Code Agent Teams (multi-agent
parallelism). The model is mainstream; Reyn's differentiation is the
event-sourced / deterministic-replay / permission-gated persistence beneath it.
