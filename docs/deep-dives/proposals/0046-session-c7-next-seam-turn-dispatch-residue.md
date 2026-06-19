# FP-0046: Session C7 — next seam (turn-dispatch residue) + remaining-cluster roadmap

**Status**: proposed (design-review only — no cut)
**Proposed**: 2026-06-19
**Author**: e2e-coder session (#1792 / FP-0044 C-series, Stage-2)
**Gate**: owner-gated. Picks the next `Session` method-cluster cut after C6
(#1804, merged). Lead review → owner review → then a PR. No code is cut here.

> Line numbers are as-of `origin/main` post-C6 (session.py 4793 LOC). Method /
> section names are the authoritative anchors (session.py moves across the
> C-series).

## Context

C6 (#1804) collapsed the 6 forwarding-residue methods to `ContextBudgetAdvisor`
+ `RouterHistoryBuffer` (behavior-preserving, replay-green). FP-0044 §(d) named
the remaining clusters: intervention coordination / persistence-journal / turn
dispatch / history-context assembly. The history-context residue is now gone
(C6). This doc scores the remaining three and recommends the cleanest next cut.

## Candidate scoring

Two *kinds* of remaining cut exist, and they differ sharply in risk:

- **Residue-collapse** (like C6): thin forwarders to a collaborator that
  already holds the logic → rewire callers direct, delete the forwarder.
  Behavior-preserving, replay-green, near-zero risk.
- **Real-logic extraction**: move genuinely-inline Session logic (and possibly
  state) into a *new or existing* collaborator. Behavior-preserving but a real
  decoupling — higher care, its own dedicated seam doc.

| Cluster | Kind | Collapsible | Risk | Value |
|---|---|---|---|---|
| **turn-dispatch** (RouterLoopDriver forwarders) | residue | 3 methods | very low | low (finishes the FP-0043 residue story) |
| **persistence/journal** | mixed | ~4 thin accessors; the rest (`restore_state`, `reset_for_rewind`) are load-bearing coordinator core that **stays** | low–med | low–med |
| **intervention coordination** | real-logic | ~10 methods, ~575 LOC, incl. 89 LOC `handle_intervention` / 74 LOC `_dispatch_intervention` + Session-owned `_intervention_overrides` state + bus callbacks | **high** | **high** (the real god-class reduction) |

## Recommended next cut: turn-dispatch residue collapse (C7)

**Collapse the 3 remaining pure forwarders to `RouterLoopDriver` — the same
mechanic C6 applied to the other two FP-0043 collaborators — completing
Session's residue-collapse across all three.**

### Methods → collaborator

| Session forwarder | Disposition |
|---|---|
| `_router_run_with_shrink` → `RouterLoopDriver._run_with_shrink` | **delete** — zero callers (dead) |
| `_force_close_handoff` → `RouterLoopDriver._force_close_handoff` | **delete** — zero Session-external callers (the driver calls its own; tests assert the event, not the method) |
| `_force_close_wrap_up` → `RouterLoopDriver._force_close_wrap_up` | **delete** — rewire the 2 test call-sites (`test_force_close_chat_handoff_1092`) to `session._loop_driver._force_close_wrap_up` |

**Retained** (NOT in this cut): `_run_router_loop` — forwards to
`RouterLoopDriver.run_turn` **and then** does `self._journal.cut_generation(...)`
(turn-boundary side-effect). It is genuine coordinator glue, not a pure
forward — stays on Session (same reasoning C6 used to exclude it).

### Dependency direction & construction-cycle check (C6 learning applied)

`RouterLoopDriver` is constructed at `session.py:1670`. Unlike the C6
host_adapter callbacks, these three forwarders are **not** injected as
callbacks into anything built earlier — `_force_close_*` / `_router_run_*` are
called only by the driver's own internals and (for `_force_close_wrap_up`)
tests. So there is **no construction cycle**: deleting them and repointing the
test call-sites is a pure residue collapse. Dependency stays one-directional
`Session → RouterLoopDriver`; a hop is removed, none added.

### Behavior-preserving vs pure-move

**Behavior-preserving rewire**, not a byte-identical move (same class as C6).
The forwarders are deleted and the only live callers (2 test sites) repoint to
the identical collaborator method. The LLM-facing SP/tools are untouched.

### Gate

Full CI + **replay green (no re-record** — SP/tools unchanged) + old-forwarder
call-residue grep = 0 (`scripts/verify_package_move.py` / `._<method>()` grep) +
dead-method safety re-confirm (`_router_run_with_shrink` / `_force_close_handoff`
zero live callers). Session ≈ −12 LOC, −3 methods.

## Roadmap: the remaining clusters (for owner sequencing)

This C7 cut is **clean but small** — it finishes the residue story. The
substantive god-class reduction is **intervention coordination**, which is a
*real-logic extraction*, not residue, and deserves its own dedicated seam doc:

- **Intervention coordination** (highest value): ~10 methods incl. the 89 LOC
  `handle_intervention` and 74 LOC `_dispatch_intervention`. `InterventionRegistry`
  + `InterventionHandler` already exist and own the registry/listener state, but
  Session still owns `_intervention_overrides` and the routing logic, and the
  C4 buses + crash-recovery call back into Session. The key design decision (for
  its own doc): does the override state + routing move into an
  `InterventionCoordinator`, with Session holding a ref, or stay with the
  collaborator holding a Session back-ref? This is the first cut where **state
  ownership moves** — owner-level, careful.
- **Persistence/journal** (low–med): a few thin accessors (`attach_workspace_store`,
  `attach_anchor_store`, `current_snapshot`) collapse to `snapshot_journal`, but
  `restore_state` / `reset_for_rewind` are load-bearing lifecycle that **stay**
  as coordinator core. Small net — could ride a later cut.

**If owner prefers to skip the small C7 and go straight to the high-value
intervention extraction, that is reasonable** — I'll write the dedicated
intervention seam doc instead. This doc recommends C7 as the *cleanest* next
cut by FP-0044's literal criterion, but surfaces the value trade-off for the
owner's call.

## Related

- FP-0045 (C6 — the residue-collapse this finishes; the construction-cycle
  learning applied in the cycle-check above)
- FP-0044 §(d) (cluster candidates)
- #1792 (C-series tracking)
