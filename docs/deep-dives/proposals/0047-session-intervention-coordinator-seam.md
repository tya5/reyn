# FP-0047: Session intervention-coordination seam — the first state-ownership move

**Status**: proposed (design-review only — no cut)
**Proposed**: 2026-06-19
**Author**: e2e-coder session (#1792 / FP-0044 C-series, Stage-2)
**Gate**: owner-gated. This is the **first** C-series cut that moves *state* (not
just methods) off `Session`, so the design is settled here before any cut.
Lead review → owner review → implement. No code is cut by this doc.

> Line numbers are as-of `origin/main` post-C7 (session.py 4781 LOC); method /
> section names are the authoritative anchors.

## Context

C6 + C7 closed the FP-0043 *forwarding-residue* story (thin shims to collaborators
that already held the logic). Intervention coordination is a different kind of
cut: `InterventionRegistry` + `InterventionHandler` already exist as Session
collaborators, **but Session still owns real coordination logic and one piece of
state** (`_intervention_overrides`), and it reaches into the registry's *private*
state. This is a **real-logic + state extraction**, not a residue collapse —
higher care, and the first time ownership of state moves.

## What is on Session today

- **State**: `self._intervention_overrides: dict[str, RequestBus]` — the only
  intervention state not already in a collaborator (the registry owns the
  active/stalled/listener queues; the handler owns delivery).
- **Override accessors** (6, thin over the dict): `register_intervention_override`,
  `unregister_intervention_override`, `has_intervention_override`,
  `get_intervention_override`, `intervention_override_count`.
- **`_dispatch_intervention`** — the orchestration: (1) override-observer
  side-effects (`override.on_dispatch(iv)`), (2) origin-pin stall check, (3)
  delegate to `InterventionHandler.dispatch`. **This is real logic, not a thin
  wrapper** (its docstring's "thin wrapper" line is inaccurate).
- **`handle_intervention`** — the Agent-layer entry (self-answer / parent-delegate
  / dispatch routing).
- Misc: `answer_pending_intervention`, `claim_pending_intervention`,
  `discard_pending_intervention`, `list_stalled_interventions`,
  `register/unregister_intervention_listener`, `try_self_answer`,
  `consume_buffered_intervention_answer` — mostly delegate to registry/handler.

## Design center: the encapsulation coupling (lead's required decision)

`_dispatch_intervention` reaches into the registry's **private** state at exactly
**two** points:

1. read — `iv.origin_channel_id not in self._interventions._listeners`
2. write — `self._interventions._stalled[iv.id] = iv` (parks a *never-active* iv)

This is the coupling to resolve. Two options:

- **(a) Add clean registry APIs** *(recommended)* — the coupling is only 2 calls;
  give `InterventionRegistry` two methods and the coordinator uses them:
  - `has_listener(listener_id: str) -> bool` (the registry already has
    `register_listener` / `has_active_listener`; this is the by-id query)
  - `park_stalled(iv: UserIntervention) -> None` (distinct from the existing
    `mark_stalled(iv_id)`, which transitions an *active* iv; this parks a fresh
    one)
  Keeps the registry the sole owner of its queues (proper encapsulation), and the
  coordinator depends only on the registry's public surface.
- (b) Move the stall-check (and the `_stalled` ownership) into the coordinator —
  **rejected**: it would split ownership of the stalled queue between the registry
  (`mark_stalled`/`list_stalled`/`discard_stalled`) and the coordinator, which is
  worse encapsulation than (a), not better.

**Recommendation: (a).** It is the smaller change *and* the cleaner boundary.

## Proposed collaborator: `InterventionCoordinator`

A new `reyn.runtime.services.intervention_coordinator.InterventionCoordinator`
that **owns**:

- `_intervention_overrides` (the dict moves here) + the 6 override accessors.
- `is_override_active(run_id) -> bool` — the override-active predicate. Today this
  logic is **duplicated**: in `_dispatch_intervention` AND in
  `ChatInterventionBus.deliver` (`session_buses.py`, which reads
  `self._session._intervention_overrides` directly). The coordinator exposes one
  predicate; both call it (de-dup + removes the bus's reach into Session state).
- the `_dispatch_intervention` orchestration (override-observe → stall-check via
  the new registry API → `handler.dispatch`).

It **holds refs** to the existing `InterventionRegistry` + `InterventionHandler`
(constructed just after them on `Session`). `Session` keeps thin delegating
methods for the external call surface (the buses call
`session.handle_intervention` / `session._dispatch_intervention`); those forward
to the coordinator. `handle_intervention` may stay on Session as the Agent-layer
entry that calls into the coordinator — TBD in review (it touches agent-role
routing, arguably coordinator-core).

## Construction-cycle check (C6 learning)

**No cycle.** `InterventionRegistry` (`:1310`) + `InterventionHandler` (`:1325`)
are built early; the coordinator is built right after them, holding their refs.
The `ChatInterventionBus` instances are built later (`:1517` / `:2258` / `:4653`)
and call the coordinator (via Session) **at runtime**, not at construction — so
this is *not* the host_adapter-style construction cycle C6 hit. The coordinator
needs no callback into a not-yet-built object.

## Behavior-preserving vs pure-move + gate

This is **NOT** a pure / byte-identical move — logic and state relocate, and two
new registry APIs replace private-state access. So the gate is stronger than the
residue cuts:

- **Replay green** (no re-record): the dispatch path does not change SP/tools.
- **Unit pins for the relocated behavior** (the real risk surface):
  - override-observer side-effects still fire **before** dispatch and are
    best-effort (a raising `on_dispatch` must not block dispatch);
  - origin-pin stall: an iv whose `origin_channel_id` has no live listener is
    **parked stalled** (now via `registry.park_stalled`) and awaits its future;
  - `ChatInterventionBus.deliver`'s override-active **skip** is unchanged after
    it switches to `coordinator.is_override_active`.
- The existing intervention suites are the behavior net: `test_pending_intervention_268`,
  `test_intervention_*`, `test_a2a_*`, `test_chat_bus_stamping_268_continued`,
  `test_intervention_resume_e2e`.
- Straggler: `verify_package_move.py` for any moved symbol + grep that no caller
  reaches `_intervention_overrides` / `_interventions._stalled` /
  `_interventions._listeners` outside the coordinator after the cut.

## Open questions for lead + owner

1. **Encapsulation fix (a) vs (b)** — recommend (a) (2 clean registry APIs).
2. **State home**: `_intervention_overrides` moves *into* the coordinator (the
   coordinator owns it), vs the coordinator holding a `Session` back-ref to read
   it. Recommend **move into the coordinator** — it is intervention state, and no
   non-intervention code needs it (the only external reader, the bus, switches to
   `is_override_active`).
3. **`handle_intervention` placement** — stays on Session (Agent-layer entry) or
   moves to the coordinator? It touches agent-role routing; lean *stays*, calling
   the coordinator. Confirm in review.
4. **Cut size**: do this as one PR (state + 6 accessors + dispatch + 2 registry
   APIs + bus de-dup), or split (registry APIs first, then the move)? Lean a
   single behavior-preserving PR with the unit pins, since splitting leaves a
   half-migrated override predicate.

## Roadmap after this

The remaining FP-0044 cluster is **persistence/journal**, which is mostly
load-bearing coordinator core (`restore_state`, `reset_for_rewind` stay) with a
few thin accessors — a small, low-value cut that can ride a later PR or be
folded into a cleanup. After intervention, `Session` is substantively a
lifecycle coordinator holding collaborators — the FP-0043/0044 goal.

## Related

- FP-0045 (C6 — construction-cycle learning applied here)
- FP-0046 (C7 — the residue cuts this graduates from)
- FP-0044 §(d) (cluster plan)
- #1792 (C-series tracking)
