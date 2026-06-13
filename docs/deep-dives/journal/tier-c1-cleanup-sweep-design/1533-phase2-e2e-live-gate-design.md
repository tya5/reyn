# #1533 Phase-2 e2e live gate (design)

**Status**: design-first, pending lead review (then impl). Author: dogfood-coder.
The Phase-2 (A)-equivalent of the Phase-1 deterministic gate
(`tests/test_live_rewind_gate.py`).

## The scope question (lead): does a session-level gate add value over 2a-2?

**Yes — different blind-spot.** Precisely:

- **2a-2's `test_checkout_back_revives_lineage_two_substrate`** proves checkout /
  checkout-back **correctness** on both substrates (workspace v2→v3 + inbox), but
  the captures are **manually constructed** (`_put` + `ws.capture(seq)`). It does
  not drive the production turn loop.
- The **Phase-1 gate** proves real-turn `_run_router_loop` → genuine
  `cut_generation` auto-capture → `rewind_to` (undo) reverts both substrates. It
  stops at undo — no fork, no post-rewind continue.

The Phase-2 gate closes the **composition** the other two don't:
**(real `_run_router_loop` cut_generation auto-capture) × (checkout branch-switch
to an abandoned seq + a post-fork *continue* turn through the real loop +
checkout-back)**. New wiring it exercises that nothing else does:

1. captures come from **genuine** turns (if `_run_router_loop` ever stops calling
   `cut_generation`, a manual-capture test stays green — the Phase-1 gate's
   rationale, extended to fork);
2. the session is **live-usable after a rewind** — a real turn runs *through the
   production loop* on the new active branch post-undo (the Phase-1 gate never
   drives a turn after rewind; fork UX depends on "fork, then keep working");
3. **checkout to an abandoned seq** revives a lineage whose captures were
   real-turn-produced (not hand-built), and a subsequent checkout-back follows
   the post-fork-continue lineage.

So: 2a-2 = checkout correctness (unit-of-substrate); Phase-2 gate = production
wiring composition (end-to-end). Both needed; neither subsumes the other.

## Gate scenario (mirrors the Phase-1 gate structure)

Real `AgentRegistry` + `ChatSession` + `StateLog` + real git; `_FakeTurnDriver`
(no-LLM) swapped in so the genuine `_run_router_loop` + `cut_generation` fire; the
only simulated effect is the per-turn file write (same as Phase-1).

```
turn A  → file v1, runtime [A], genuine cut_generation @ seqA
turn B  → file v2, runtime [A,B], cut_generation @ seqB
rewind_to(seqA)            # undo → B's branch becomes abandoned (a dead branch)
turn C  → file v3, runtime [A,C], cut_generation @ seqC   # REAL turn on the post-undo active branch
checkout(seqB)             # branch-switch to the abandoned B lineage
  → assert workspace == v2, runtime == [A,B]              # fork revived from real captures
checkout(seqC)             # checkout-back to the C lineage
  → assert workspace == v3, runtime == [A,C]
```

Assertions on **both substrates** at each checkout (workspace file on disk +
runtime: on-disk snapshot AND live in-memory session — the two distinct wirings
the Phase-1 gate already separates).

## Open sub-question for lead

- **act-turn (2a-3) in the gate?** The act-turn rewind path
  (`plan_for_act_turn_rewind`) is runtime-only and feeds `OSRuntime.run(resume_plan=)`
  — a *different* launch seam than the turn-boundary `checkout`. I lean **keep the
  Phase-2 gate to the boundary-granularity fork/checkout-back full-path** (the
  (A)-equivalent), and cover act-turn rewind separately if a live gate is wanted
  (it has no workspace substrate, so it isn't a "both-substrate" composition).
  Flag if you want act-turn folded into this gate.

## Plan

Design → #1533 comment → lead review/lock → impl (`tests/test_live_fork_gate.py`
or extend `test_live_rewind_gate.py`), real-instance, `-W error::RuntimeWarning`,
tier-audit first-line `"""Tier 2: ..."""`. (B) tmux live UX = tui-coder (P1-P3
after 2b); I stay on the deterministic (A)-equivalent.
