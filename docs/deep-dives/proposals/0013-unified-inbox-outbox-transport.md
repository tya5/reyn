# FP-0013: Unified Inbox/Outbox Transport Abstraction — Collapse CUI vs MCP/A2A Skew

**Status**: **accepted** (= ADR-A starvation feasibility green-light, 2026-05-11)
**Proposed**: 2026-05-11
**Author**: 2026-05-11 design discussion (post FP-0012 R-A2A-COMPLETION-DRAIN retest)
**Trigger**: FP-0012 retest F1 finding (= A2A endpoint bypasses `session.run()` so the
`skill_completed` inbox kind never fires for A2A-driven agents). The tactical patch
(commit `b3252be`, `drain_skill_completed_inbox`) closes the immediate gap but
preserves the underlying architectural skew this proposal addresses.

## Feasibility verification (2026-05-11)

5-track parallel investigation closed open question ADR-A:

- **Track 1 (archaeology)**: Bypass was empirically observed in commit `a5678c1`
  (2026-05-07). A2A inherited it uncritically (~45 min later) — uvicorn / pure
  asyncio surface, never had the original problem.
- **Track 2 (mechanics)**: Root cause was anyio task-group structured-concurrency
  cancellation cascade + buffer-0 memory-stream rendezvous, NOT generic asyncio
  unfairness. Pumping collapses 2 tasks → 1 task; mechanically eliminates the
  failure mode.
- **Track 3 (industry)**: Request-handler pumping is industry-standard
  (LangGraph `astream`, Strawberry GraphQL subscriptions are direct precedents).
- **Track 4 (baseline repro)**: Built 3 progressively-closer harnesses
  (asyncio / anyio / real `mcp.server.Server` over in-memory JSON-RPC). All 3
  passed; starvation did NOT reproduce. Subprocess + real stdio byte transport
  not exercised — deferred to residual verification.
- **Track 5 (pumping prototype)**: Implemented `ChatSession.run_one_iteration` +
  `send_to_agent_impl_pumping`. 4/4 spike tests pass, 334 regression tests green,
  ~100ms slower than bypass.

Synthesis: `docs/deep-dives/journal/feature-verify/2026-05-11-adr-a-starvation-feasibility/synthesis.md`.

**Resolution**: green-light proceed. Cost estimate refined — the core
decomposition is SMALL-MEDIUM (~25+80 lines); the LARGE estimate now reflects
verification + soak rather than the code change. **3 residual verifications**
required as preconditions for the bypass-deletion commit (not for accepting
the proposal):

1. Subprocess + real stdio probe (`scripts/mcp_probe.py` against pumping path).
2. anyio CancelledError soak (mid-call disconnect).
3. `_receive_loop` heartbeat instrumentation during >5s LLM call.

**Naming refinement adopted**: pump primitive renamed
`session.run_until_reply(reply_to: TransportRef) -> OutboxMessage` (Track 3 suggestion,
mirrors LangGraph `astream`'s `__anext__`).

**Migration ordering refinement**: A2A migration moves first (= never needed
the bypass per Track 1); MCP follows after subprocess soak.

---

## Summary

Reyn's design thesis: **an agent has one inbox (= incoming) and one outbox (= outgoing); the
identity of the sender / receiver (user / peer agent / MCP client / A2A peer) is transparent
to the agent body**. The agent's job is to process inbox messages and emit outbox messages
with a `reply_to` envelope; a separate **transport layer** routes inbox/outbox to / from the
real wire format (TUI, MCP stdio, A2A HTTP, peer-agent inbox).

The current implementation breaks that symmetry: CUI (`reyn chat`) is the canonical path
that drives `session.run()` over the inbox, but MCP (`reyn mcp serve`) and A2A
(`reyn web` FastAPI router) bypass `session.run()` entirely and call
`ChatSession._handle_user_message` inline, then harvest `session.history` directly. The
bypass exists for a real technical reason (= MCP SDK stdio transport starves an
`asyncio.create_task`-spawned background coroutine), but as a consequence:

- Every new inbox kind (e.g. FP-0012's `skill_completed`) needs a parallel drain in the
  bypass path — already two patches deep (`running_plans` from G27 batch 17,
  `running_skills` + `drain_skill_completed_inbox` from R-A2A-COMPLETION-DRAIN).
- Multi-agent relay semantics (`_PendingChain`, `agent_request` / `agent_response`) are
  expressible only through the CUI inbox loop; A2A peers can't naturally participate.
- Reply routing is implicit (= CUI prints outbox to terminal, MCP harvests history) rather
  than carried in the message envelope, so an agent can't address its reply at a specific
  transport.

This proposal converts the inbox into the **single intake channel for every transport** and
introduces a `TransportRef`-tagged outbox so a routing layer can fan replies back to the
correct destination. `session.run()` becomes the only consumer; transport-specific code
shrinks to put/route adapters.

---

## Motivation

### The thesis Reyn was built on

From the start, peer agents communicate via `agent_request` / `agent_response` messages
that land in the recipient's inbox identical to a `user` message, modulo the `kind` field.
The router LLM doesn't know (and shouldn't need to know) which peer typed at it — it just
processes the message and emits a reply. This symmetry is what makes multi-agent
delegation, A2A, and (eventually) external MCP clients all work through the same
RouterLoop + tool surface.

The agent's contract should be:

```
                      ┌───────────────────────────────────────┐
                      │ Transport adapters (= I/O only)       │
                      │                                       │
TUI ─────────────────►│ inbox.put({kind="user",               │
MCP stdio ───────────►│            payload=...,               │──► inbox (queue)
A2A HTTP ────────────►│            reply_to=<TransportRef>})  │
peer agent ──────────►│ (= "agent_request" with reply_to =    │
                      │     other-agent inbox)                │
                      └───────────────────────────────────────┘
                                       │
                                       ▼
                       session.run() — sole consumer
                                       │
                                       ▼
                                outbox.put(
                                  OutboxMessage(text=...,
                                                reply_to=<envelope.reply_to>))
                                       │
                                       ▼
                      ┌───────────────────────────────────────┐
                      │ Routing layer (= reply fan-out)       │
                      │ dispatch by reply_to discriminator:   │
                      │   TUI       → renderer                │
                      │   MCP req   → JSON-RPC response       │
                      │   A2A req   → HTTP response           │
                      │   AgentRef  → peer.inbox.put(         │
                      │                 kind="agent_response")│
                      └───────────────────────────────────────┘
```

### Where the current implementation breaks symmetry

`mcp_server.send_to_agent_impl` driving `_handle_user_message` inline is not a small
shortcut — it forks the entire turn lifecycle:

| Surface | CUI (`reyn chat`) | MCP / A2A (`send_to_agent_impl`) |
|---|---|---|
| Inbox consumer | `session.run()` long-lived task | inline drain per request |
| Turn boundary | one inbox kind processed | `send_to_agent_impl` return |
| `skill_completed` handling | natural pickup by inbox loop | needs explicit `drain_skill_completed_inbox` |
| `running_plans` completion | natural pickup | needs explicit `await asyncio.gather` |
| Concurrency | inbox = serialization point | per-agent `asyncio.Lock` (= separate) |
| Reply routing | implicit (outbox → TUI) | implicit (`session.history` harvest) |
| Multi-agent relay | works natively via `_PendingChain` | impossible from A2A entry |

The bypass was justified by the MCP SDK stdio transport's starvation behaviour: an
`asyncio.create_task`-spawned `session.run()` coroutine doesn't get scheduled while the
request handler is awaiting an LLM call. That's a real constraint, but the **fix should
not be to bypass the inbox**; it should be to drive `session.run()` on the same task that
holds the event loop.

### Evidence the skew is accumulating debt

1. **R-A2A-COMPLETION-DRAIN** (= commit `b3252be`, 2026-05-11): every new inbox kind FP-XXXX
   introduces requires a parallel drain handler in `send_to_agent_impl`. The pattern was
   already paid once for `running_plans` (G27 batch 17, ADR-0023 §2.1.1); this is the
   second time.
2. **History harvesting fragility**: `_new_agent_history_entries` filters by `chain_id` to
   scope reply harvest to the caller's chain, because concurrent `send_to_agent_impl`
   calls would otherwise cross-talk. The single-consumer inbox model wouldn't need this.
3. **A2A peers can't natively participate in multi-hop chains**: if agent X (driven via
   A2A) needs a reply from agent Y, today's path is "spawn delegate, harvest history".
   The unified design lets agent Y's `agent_response` arrive at X's inbox just like a
   peer agent message.
4. **Test surface duplication**: `test_send_to_agent_waits_for_plan_terminal_text`,
   `test_send_to_agent_drains_skill_completed_inbox`, and any future async-completion
   inbox kind needs its own bypass-path regression net.

---

## Proposed implementation

### Component A — `TransportRef` discriminated union (= reply-to schema)

New value object in `src/reyn/chat/transport.py`:

```python
TransportRef = (
    | TuiRef()                                    # local terminal renderer
    | McpRef(request_id: str)                     # one MCP JSON-RPC request
    | A2aRef(request_id: str)                     # one FastAPI A2A request
    | AgentRef(agent_name: str, chain_id: str)    # peer agent inbox
    | SystemRef()                                 # internal (= skill_completed
                                                  #   etc., no external sender)
)
```

`InboxMessage` payload gains `reply_to: TransportRef` (= optional during migration; required
post-migration). `OutboxMessage` gains `reply_to: TransportRef` for routing.

### Component B — `session.run_one_iteration()` (= pumping model)

Decompose `session.run()`'s `while True: kind, payload = await _consume_inbox()` into a
**single-iteration variant** that processes exactly one inbox kind and returns:

```python
async def run_one_iteration(self) -> bool:
    """Process exactly one inbox kind. Returns False on shutdown, True otherwise.

    Same handler dispatch as run(); the only difference is no while-loop. Callers
    decide when to pump again — long-lived sessions loop forever (CUI), request-
    driven sessions pump until idle (MCP / A2A).
    """
    kind, payload = await self._consume_inbox()
    if kind == "shutdown":
        return False
    if kind == "user":
        await self._handle_user_message(...)
    elif kind == "skill_completed":
        await self._handle_skill_completed(payload)
    elif kind == "agent_request":
        await self._handle_agent_request(payload)
    elif kind == "agent_response":
        await self._handle_agent_response(payload)
    return True
```

`session.run()` becomes the trivial wrapper:

```python
async def run(self) -> None:
    while await self.run_one_iteration():
        pass
    await self._drain_on_shutdown()
```

### Component C — `RoutingLayer` (= outbox → transport fan-out)

A small `RoutingLayer` class subscribes to `outbox` and dispatches each `OutboxMessage`
based on `reply_to`. Transport-specific adapters register handlers:

```python
class RoutingLayer:
    def register(self, ref_type: type[TransportRef], handler: Callable): ...
    async def dispatch(self, msg: OutboxMessage) -> None:
        handler = self._handlers[type(msg.reply_to)]
        await handler(msg)
```

CUI registers `TuiRef → renderer.print`; MCP server registers
`McpRef → resolve_request_future(request_id, msg.text)`; A2A router registers
`A2aRef → resolve_request_future(request_id, msg.text)`; peer-agent delegate
registers `AgentRef → other_session.inbox.put(...)`.

### Component D — `MessageBus` for request/reply correlation

MCP / A2A request handlers can't simply put-and-wait — they need to know when the
request is "complete" (= all narration for that request has been emitted). A
per-request correlation channel:

```python
class MessageBus:
    async def request(
        self, agent: ChatSession, kind: str, payload: dict,
        reply_to: TransportRef, *, timeout: float,
    ) -> list[OutboxMessage]:
        """Put a message on `agent.inbox` tagged with `reply_to`, pump
        `agent.run_one_iteration()` from the same task until either:
        - the routing layer reports all reply_to=<this ref> outbox
          messages have been drained AND no in-flight tasks remain
          (running_skills / running_plans / pending_chains for this
          chain), OR
        - the timeout fires.

        Returns the list of OutboxMessages that were emitted for this
        request. Pumping from the same task sidesteps the MCP SDK
        stdio starvation problem (= no background task to starve).
        """
```

CUI just runs `await session.run()` (= equivalent to `while True: pump`).

MCP / A2A become:

```python
# mcp_server.send_to_agent_impl, post-migration
async def send_to_agent_impl(registry, *, agent_name, message, timeout):
    session = registry.get_or_load(agent_name)
    bus = registry.message_bus
    req_id = _new_request_id()
    replies = await bus.request(
        session,
        kind="user",
        payload={"text": message},
        reply_to=McpRef(request_id=req_id),
        timeout=timeout,
    )
    return {"reply": "\n\n".join(r.text for r in replies), ...}
```

No more inline `_handle_user_message`, no more `running_plans` / `running_skills` gather,
no more `drain_skill_completed_inbox`. All four become consequences of the bus's
"pump until quiescent" loop.

### Component E — Multi-agent relay falls out

`_PendingChain` / `agent_request` / `agent_response` simplify to: agent X's delegate tool
puts `AgentRef(other_agent, chain_id)` as `reply_to` on a message dropped into agent Y's
inbox; agent Y processes it and emits its reply with `reply_to=AgentRef(X, chain_id)`;
the routing layer wires it into X's inbox as `agent_response`. The `_PendingChain` book
becomes optional metadata for chain-timeout watchdog purposes, not load-bearing for
delivery.

---

## Open design questions (delegate to ADR)

These are the non-obvious sub-decisions that warrant a follow-up ADR (or ADRs) once this
proposal is accepted in principle:

1. **ADR-A: Starvation feasibility verification.** Does pumping
   `run_one_iteration()` from the MCP request-handler task actually eliminate the
   starvation observed in pre-FP-0013 code? Empirically validate with a stdio e2e test
   before committing to migration. If pumping still starves under some scenario, fall
   back to per-request `asyncio.Task` with explicit yields.
2. **ADR-B: `TransportRef` schema + serialization.** Are refs purely runtime objects, or
   do they need to survive crash recovery (= snapshot persistence)? `AgentRef` likely
   yes (for in-flight cross-agent chains); `McpRef` / `A2aRef` likely no (transport
   request dies with the process).
3. **ADR-C: Routing layer ↔ outbox lifecycle.** Today `outbox` is a per-session
   asyncio.Queue consumed by the CUI renderer. Post-migration, who owns the routing
   layer (= per-registry singleton vs per-session), and how does it interact with
   outbox slash-command echoes (`/skill list`, `/tasks` output) that have no external
   reply_to?
4. **ADR-D: Migration ordering.** Can `run_one_iteration` ship alongside the existing
   `run()` loop (= both available, transports pick one) for a transitional period? Or
   does the bypass need to be deleted in lockstep to avoid two divergent paths drifting
   further?
5. **ADR-E: Quiescence detection for `MessageBus.request`.** "All replies for this
   reply_to have been drained" is straightforward; "no in-flight tasks remain for this
   chain" needs a precise predicate over `running_skills` / `running_plans` /
   `pending_chains` filtered by chain_id. Cross-chain interference (= concurrent
   request to the same agent) must not falsely block quiescence.
6. **ADR-F: Backward compatibility for tactical patches.** Should the tactical
   `drain_skill_completed_inbox` be deleted when FP-0013 lands, or kept as a fallback
   if `MessageBus` is unavailable? Default plan: delete in the same commit as the
   bypass.

---

## Dependencies

- **FP-0012 (LANDED 2026-05-10)** — provides `skill_completed` as the first
  asynchronous inbox kind that surfaced the skew. Without FP-0012, the bypass
  worked accidentally well.
- **R-A2A-COMPLETION-DRAIN (LANDED 2026-05-11, commit `b3252be`)** — tactical
  patch; FP-0013 obsoletes it and the patch is removed during migration.
- **PR21 (LANDED)** — inbox WAL semantics that `run_one_iteration` must continue
  honoring. No schema change anticipated.
- **ADR-0023 (LANDED)** — plan-mode async dispatch; the `running_plans` await
  pattern in `send_to_agent_impl` (G27 batch 17) is the second tactical patch
  this proposal subsumes.

No new external dependencies.

---

## Migration plan (high-level)

1. Land `TransportRef` schema (additive, no behavioural change).
2. Land `run_one_iteration` alongside `run()` (= refactoring, both green).
3. Land `RoutingLayer` + register `TuiRef` handler matching today's renderer.
4. Land `MessageBus.request` with `MessageBus.request_inline_pump` mode for MCP/A2A.
5. **Feasibility checkpoint**: verify MCP stdio e2e no longer starves under the
   pumping model. Block on this — if pumping still starves, escalate to ADR-A
   resolution.
6. Migrate `send_to_agent_impl` to `MessageBus.request` (= bypass deleted).
7. Migrate multi-agent relay to `AgentRef` `reply_to` (= `_PendingChain` retains
   only chain-timeout responsibility).
8. Remove tactical patches: `drain_skill_completed_inbox`,
   `_new_agent_history_entries` chain_id filter, per-agent `asyncio.Lock`.
9. Test coverage: every transport surface gets the same set of contract tests
   (= one round-trip + completion narration + multi-hop delegation), parameterized
   over `TransportRef` variants.

---

## Cost estimate

**LARGE** (~1-2 weeks of focused work, dependent on starvation feasibility outcome).

Breakdown:

- TransportRef schema + tests: ~0.5 day
- `run_one_iteration` decomposition + behaviour tests: ~1-1.5 day (sensitive to the
  `_drain_on_shutdown` interaction)
- RoutingLayer + TUI adapter: ~1 day
- MessageBus + quiescence predicate: ~1.5-2 day (= the most subtle piece)
- Starvation feasibility verification: ~0.5-1 day (= may force redesign of pumping mode)
- MCP + A2A migration: ~1 day
- Multi-agent relay migration: ~1-1.5 day
- Tactical patch removal + symmetric test coverage: ~0.5-1 day
- ADR drafting (A-F as needed): ~0.5-1 day

Compresses with parallel sonnet on schema / TUI adapter / migration steps. The
quiescence predicate (`MessageBus.request`) is hard to parallelise — likely the
critical path.

---

## Risks

- **Starvation still bites under pumping model** — fallback plan: per-request
  `asyncio.Task` with explicit `asyncio.sleep(0)` yields injected at known
  await points. Less clean but preserves the abstraction.
- **Quiescence false-positives** — `MessageBus.request` might return before all
  narration emits if the predicate is too lax. Mitigation: conservative
  predicate + integration test matrix covering each async inbox kind.
- **CUI behaviour regression** — `run_one_iteration` must preserve every edge
  case in the current `run()` loop (shutdown signal, exception handling,
  `_drain_on_shutdown` invariants). Mitigation: keep `run()` as the trivial
  loop wrapper; behavioural tests pin it.
- **Pre-existing chitchat replay flakiness** (= observed during
  R-A2A-COMPLETION-DRAIN verification) is independent of this work; not a
  blocker but worth landing a fix before migration so the regression net is
  reliable.

---

## Related

- **FP-0012**: async skill execution — surfaced the skew via `skill_completed`.
- **R-A2A-COMPLETION-DRAIN**: commit `b3252be`, tactical patch this proposal
  obsoletes.
- **ADR-0023**: plan-mode async dispatch — first instance of "explicit await in
  the bypass path" pattern.
- **`docs/concepts/async-skill-execution.md`** — current architecture doc;
  needs rewrite when FP-0013 lands.
- **G27 batch 17 (= commit `3a59d8c`)**: the `running_plans` gather patch in
  `send_to_agent_impl` is the second tactical workaround in this surface area;
  FP-0013 subsumes it.
