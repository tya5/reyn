# #1533 2a-2 — `checkout(seq)` unified primitive (design draft)

**Status**: IMPLEMENTED on `feat/1533-2a2-checkout` (2a-1 merged in #1558). Author: dogfood-coder.
**Depends on**: 2a-1 branch-registry (`branch_ids_for` / `list_branches`, abandoned-interval-grounded). Refactors `rewind_to`.

> Landed: substrate `checkout(state_log, *, target_seq)` (unconditional) + `rewind` guarded-delegate; registry `checkout(seq)` + `rewind_to` thin wrapper; `_abandoned_intervals` docstring updated. Tests: `tests/test_registry_checkout_2a2.py` (full-path runtime + two-substrate round-trip + equivalence + preserved-guard). The interval-layer caveat below is now closed by the real-instance integration tests.

## Goal

One primitive `checkout(seq)` that subsumes Phase-1 `rewind_to` (active-branch
undo) and Phase-2 fork/branch-switch (jump to *any* seq, including an abandoned
branch's). `rewind_to` becomes the **active-node special case** of `checkout`.

## The load-bearing distinction (the real substrate work)

My first read assumed checkout-to-a-fork would need **new lineage-aware
reconstruction** (walk active root → fork point → target branch segment), because
today's `rewind_to` path reconstructs along the **active** branch
(`reconstruct` / `_restore_workspace_active` honor `is_active_seq`). **That
assumption was wrong** — verified below.

The key property: `reconstruct`, `_materialize_rewind`, and
`_restore_workspace_active` all recompute `is_active` **fresh from the full
reset-record chain on every call**. So the moment a checkout's reset-record is
appended, `is_active` *already describes the post-checkout active branch* — the
existing machinery is automatically lineage-correct with no change.

## Open question — RESOLVED by primary-evidence verification

**Reset-record semantics for branch-switch.** Candidates were (1) a single
reset-record `(R, target_n)` with the guard lifted, vs (2) an explicit
branch-pointer field. **(1) is correct and needs NO new persisted field** —
verified by exercising the production `_abandoned_intervals` / `branch_ids_for`
on a real `StateLog` (all transitions inspected directly, not extrapolated):

```
root 1..10, rewind→6, continue {12,13}:
  abandoned=[(6,11)]   active=[1..6,11,12,13]
checkout→9  (abandoned target, guard lifted, record (14,9)):
  abandoned=[(9,14)]   active=[1..9,14]           # 7,8,9 revived; 12,13 → dead branch 14
  branch_ids: 6→0  9→0  7→0  12→14  13→14          # prior (6,11) subsumed (11 ∈ (9,14))
checkout-back→12 (record (15,12)):
  abandoned=[(12,15),(6,11)]  active=[1..6,11,12,15]
  branch_ids: 6→0  12→0  9→11  11→0                # (6,11) RESURRECTS; 12 lineage = 1..6,11,12
```

The checkout-back case (where naive schemes break) composes correctly: the
latest-first walk subsumes the intervening record when its R falls inside a newer
interval, and *resurrects* an older abandonment when the subsuming record is
itself later abandoned. `N < R` always holds (target is a real prior seq), so no
degenerate interval. **No `is_active` flip-flag, no branch-pointer field needed.**

**Caveat (scope of this verification):** this confirms the *interval-composition
layer* (`is_active` / `branch_ids_for`). The full registry.checkout path
(`reconstruct` + workspace restore + session re-adopt) is expected-correct *by
derivation* (all recompute `is_active`) but is **not yet** exercised end-to-end —
that needs the real-instance integration test (below) as primary evidence.

## Proposed shape (simplified by the finding)

```
async def checkout(self, seq: int) -> dict:
    # = rewind_to body MINUS the is_active_seq guard (retention guard kept).
    #   all-cancel / all-quiesce / single reset-record / _materialize_rewind —
    #   all unchanged; they recompute is_active from the post-checkout chain.

async def rewind_to(self, target_n: int) -> dict:
    # thin Phase-1 wrapper: is_active_seq(target_n) guard
    #   (RewindIntoAbandonedError) → delegate to checkout(target_n).
```

The guard moves OUT of the shared core into the `rewind_to` wrapper; Phase-1
callers keep `RewindIntoAbandonedError` on an abandoned target. The
`_abandoned_intervals` docstring's "Phase-1 active-target guard" premise note
must be updated (guard-lifted composition is still well-defined, per above).

## Test plan (2a-2)

- checkout to active seq == rewind_to (behavioral equivalence; existing 1c tests).
- checkout to abandoned seq → reconstructs target-branch content (the dead
  branch's snapshot), abandons prior active head; `list_branches` reflects the
  swap.
- round-trip: A→fork B→checkout back to A→checkout B again (N-way, no leakage).
- workspace substrate follows the target lineage (not active) — real git store.
- retention guard shared with rewind_to.
- `-W error::RuntimeWarning`, tier-audit first-line `"""Tier 2: ..."""`.
