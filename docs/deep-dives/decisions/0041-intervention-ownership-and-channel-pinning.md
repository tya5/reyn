# ADR-0041: intervention ownership and channel pinning — supersedes ADR-0034 Components 1–3

**Status**: Accepted — **implemented** (#268 Phase 1/2 2026-05-20, #292 α, #267 Gap 5).
This ADR is written after the fact (2026-08-10) to close the gap between ADR-0034's Decision text
and what shipped; see #4016.
**Supersedes**: [ADR-0034](0034-a2a-task-lifecycle.md) **Components 1, 2 and 3 only**. ADR-0034's
Components 4 (router endpoints) and 5 (Agent Card capabilities), its Context and its Considered
alternatives all stand unchanged.

## Context

ADR-0034 shipped A2A async tasks with a **chain-scoped intervention override**:
`ChatSession.register_intervention_override(chain_id, bus)` made `ask_user` inside a skill running
under that `chain_id` route to an `A2AInterventionBus` instead of the default bus, and
`RunEntry` held the pending `UserIntervention` plus its buffered question text.

Two independent investigations then falsified that shape.

**#292 — the override was a bypass, not a route.** An implementation trace showed
`_dispatch_intervention`'s override branch **returned** `await override.request(iv)`, skipping
`_intervention_handler.dispatch` entirely. Everything that hangs off the handler was therefore
forfeited for A2A interventions **by construction**:

```
❌ not added to InterventionRegistry._active
❌ no intervention_dispatched WAL event
❌ not written to AgentSnapshot.outstanding_interventions
❌ not eligible for the buffered_intervention_answers consume on restart
❌ not visible to #268's cross-channel observe / discard / claim API
✓  stored only in RunEntry.pending_intervention
```

On restart `RunRegistry` restored the intervention with a **fresh future**, while the coroutine
that had been awaiting the original future was dead. A peer's answer resolved a future nobody was
waiting on, and the ChatSession side had no record the intervention ever existed — so there was
no second object to "re-bind" to. The problem was not duplication; it was that **A2A intervention
state lived outside ChatSession's authoritative machinery**.

**#268 — one user reaches one agent through several channels.** Three scenarios drove this:
a task started over A2A is invisible in a TUI opened afterwards (the override bypasses the
outbox, so the operator can see the skill running but not what it is asking); a peer client that
crashes strands a `input-required` run with no way to answer it from anywhere else; and a TUI
session plus a concurrent A2A request are, in practice, two parallel sessions inside one
ChatSession that cannot see each other.

## Decision

### D1. ChatSession owns every intervention's lifecycle; the A2A bus decorates, never replaces

`_dispatch_intervention`'s override branch changes from *replace the dispatch* to **decorate the
dispatch with side effects** (#292 option α). The bus's entry point is therefore
`on_dispatch(iv)`, not `request(iv)`, and it **does not await `iv.future`** — awaiting belongs to
the handler that owns the lifecycle.

Consequently every guarantee ChatSession ships for local interventions — registry membership, the
`intervention_dispatched` WAL event, snapshot inclusion, restart-resume — applies to A2A
interventions too, because there is no longer a second path.

### D2. An intervention is pinned to its origin channel

`A2AInterventionBus` exposes `channel_id` = `a2a:<run_id>` and stamps it onto
`iv.origin_channel_id` at dispatch. Routing is by **channel**, not by chain.

The channel's lifetime is the request: `mcp/server.py` registers the listener before
`bus.request(...)` and unregisters it in `finally`.

An intervention whose origin channel is closed or lost **stalls** — it remains at the agent layer
rather than being delivered anywhere. Other channels may **observe** a stalled intervention and
take it over by an **explicit redirect**; nothing is fanned out implicitly. (The rejected
alternative — fan-out with first-wins — is retained in #268's body as an audit trail.)

### D3. `RunEntry` is a minimal index; intervention state is not in it

`pending_intervention` and `question` are removed from `RunEntry` (#292 α): intervention state
lives in `Session._interventions`, and the bus mirrors only `status="input-required"` onto the
entry, plus the webhook POST.

`RunEntry` **is** persisted (#267 Gap 5 — every mutation atomically rewrites the file so a
server-process restart can reload), which falsifies ADR-0034's "lifetime = process lifetime;
crash recovery for async tasks is a follow-up FP". Retention is bounded by `prune_terminal`
(terminal entries older than a 24-hour window; a `running` / `input-required` run is never pruned
regardless of age).

## Consequences

**Desirable**

- One intervention machinery instead of two; the restart-resume guarantees stop depending on
  which transport started the run.
- A stalled intervention is visible and claimable from another channel, which is what makes the
  "same agent, several channels" experience honest rather than two invisible parallel sessions.
- Fan-out is rejected explicitly, so no answer race exists to resolve.

**Undesirable / accepted**

- Channel identity (`a2a:<run_id>`) is a string convention, not a typed value. It coexists with
  the routing-key convention (`<transport>:<native_id>`) that
  [ADR-0040](0040-task-as-os-concept.md) D6 builds on; unifying them is not attempted here.
- The bus keeps a narrow write to `RunEntry` (status mirroring), so "ChatSession owns the state"
  is true of the intervention itself, not of every derived status field.

**Documentation debt this ADR closes**

ADR-0034's Components 1–3 described the pre-#292 shape for roughly three months after it stopped
being true. The mechanism was replaced on 2026-05-20 (#268 Phase 2) and the now-dead
`register_intervention_override` was deleted on 2026-07-03 as collateral of #2435's skill/phase
decouple — **six weeks after** its replacement landed. No capability was ever lost; only the
record was wrong. The verification that established this is recorded in #4016.

## References

- #292 — RunRegistry refactor: minimal index + ChatSession-derived state (option α)
- #268 — iv channel routing: origin-pinned + stall + explicit redirect
- #267 Gap 5 — RunRegistry persistence
- #2435 — removal of the by-then-dead `register_intervention_override`
- #4016 — the drift audit that produced this ADR
- Implementation: `src/reyn/interfaces/web/a2a_intervention.py`,
  `src/reyn/interfaces/web/run_registry.py`, `src/reyn/mcp/server.py`,
  `src/reyn/runtime/session.py`
