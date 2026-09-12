# ADR-0049 (#5890) — refine ADR-0044's measure to non-summary length, record ADR-0048 §2's implementation gap, and note `ROOM_FLOOR` in ADR-0048 §6's scope

**Status**: **ACCEPTED**. lead-coder, [#5890](https://github.com/tya5/reyn/issues/5890#issuecomment-5643138834) — an escalation to the owner on this same content was withdrawn after owner feedback (verbatim, in-chat): 「ドキュメントの管理はあなた達に任せてるのに私の判断必要な理由がわからん」. What follows changes no decision's *direction*, only how ADR-0044's own measure is written and what ADR-0048 records about the code it describes — the class of edit the same comment thread identifies as ours to make, not the owner's.
**Refines**: [ADR-0044](0044-overflow-recovery-ladder.md) (Decision 4's measure) and [ADR-0048](0048-compaction-ratio-target-and-oversized-tail-order.md) (§2, §6). Neither ADR's own body is edited — both stay immutable; this ADR records the refinement and the gap alongside them.
**Track**: #5890.

## Context

Two things surfaced while closing out ADR-0048: ADR-0044's own termination measure counts more than the code actually needs it to, and the current `engine.py` does not yet implement the ordering ADR-0048 §2 decided. Neither is a reason to rewrite either ADR — a *deciding* doc's claim an implementation hasn't caught up to is **unmet**, not false (CLAUDE.md). This ADR is where that distinction gets recorded, and where a measure gets stated more precisely without moving what it protects.

## Decision

### 1. ADR-0044 Decision 4's measure: count non-summary length, not raw length

ADR-0044 Decision 4 states termination rests in part on "`len(head) + len(tail)` strictly decrease[s]" (0044, Decision 4). The intent — head and tail shrink, monotonically, toward the ladder's floors — is unchanged. What this ADR refines is *what gets counted*: `len(non-summary head) + len(non-summary tail)`, not raw `len(head) + len(tail)`.

The reason is in `engine.py`'s own comment on why re-adding a summary element to `head` is safe (`src/reyn/services/compaction/engine.py:4067-4079`, verbatim):

> PR-1 reverted this exact line because it broke retry_loop's OLD termination proof (`head` is no longer monotonically non-growing once a fold can re-add an element to it — Phase 2 below could pull the just-appended element right back into raw_middle and re-fold it, an observed oscillation). What makes this safe to reintroduce is THIS PR's own Phase 1/2 change (below): they now SKIP any `role=="summary"` element when choosing what to pull (`_split_off_non_summary`) — Phase 2 structurally cannot pull the element this line just appended back into raw_middle any more, which is what the old oscillation depended on.

`_stage_refill_phase1`/`_stage_refill_phase2` (`engine.py:3539-3599`) already only pull non-summary content (`_has_non_summary` gates the trim, `_split_off_non_summary` does the split) — a fold can append a summary element to `head`, growing raw `len(head)`, without that growth ever being available for Phase 2 to re-pull, so it cannot re-enter the oscillation Decision 4's proof rules out. Raw `len(head) + len(tail)` is not actually monotone (a fold can grow it by exactly one summary element); non-summary length is what Phase 1/2's own guard makes monotone, and it is what the code has been counting on since the guard landed. The direction — head/tail shrink toward their floors — does not change; only what "shrink" counts does.

### 2. ADR-0048 §2's ordering is not yet implemented — recorded as unmet, not rewritten

ADR-0048 §2 decided: for an oversized single tail turn, spill first; if still too large after spilling, move it into `mid` so `compact()` can fold it (spill → move).

`_stage_refill_phase1` (`engine.py:3539-3571`) moves tail's non-summary content into `raw_middle` directly — there is no spill attempt on that content first. Spilling only happens afterward, inside `shrink_pool_after_overflow` (`engine.py:3215-3297`), on whatever has already landed in `raw_middle`/the offered pool. The code's actual order is move → spill, the reverse of §2's decision.

Per the hard rule this ADR is itself an instance of: ADR-0048 §2 is a *deciding* doc, the code has not caught up, and the code is what moves — §2 stays as written. This section exists to make the gap a recorded fact rather than a silent drift for the next reader of either doc.

### 3. `ROOM_FLOOR` belongs in ADR-0048 §6's scope

ADR-0048 §6 states that inside recovery, hitting a ladder floor without reaching the ratio target (Decision 1) is the expected, normal terminal — naming `MID_FLOOR` (ADR-0044's mid-side floor) as the example. `RetryLoopTerminal.ROOM_FLOOR` (`engine.py:1652-1666`, ADR-0044's room-side floor: the T_max-halved candidate can no longer fit `SP` + `new_msg` + the current summary even with `head`/`tail` at zero) is the other terminal `retry_loop` can raise, and §6's own reasoning applies to it identically — it is not a special case ADR-0048 overlooked so much as one its text happened to name only one instance of.

Concretely, this closes a gap: once `head` and `tail` are entirely summary content, `_stage_refill_phase1`/`_stage_refill_phase2` have nothing left to trim (`_has_non_summary` is false for both), so refill cannot run — the ladder proceeds straight to `_stage_halve_room`, and `ROOM_FLOOR` (not `MID_FLOOR`) is the terminal that ends the recovery pass without reaching the ratio target. §6's own logic already covers this case; this ADR records that it does, rather than leaving `ROOM_FLOOR` unmentioned as though §6 applied only to `MID_FLOOR`.

## Not decided here

- Whether/when ADR-0048 §2's ordering gets implemented, and any measurement item that lands with it — tracked in #5890, not decided by this ADR.
- Any change to ADR-0044's or ADR-0048's own decided direction — none is made or proposed here.

## References

- Issue: [#5890](https://github.com/tya5/reyn/issues/5890) — [comment](https://github.com/tya5/reyn/issues/5890#issuecomment-5643138834) grounding all three decisions above, verbatim.
- Code: `src/reyn/services/compaction/engine.py:4067-4079` (why non-summary re-append is safe), `:3539-3599` (`_stage_refill_phase1`/`_stage_refill_phase2`), `:3215-3297` (`shrink_pool_after_overflow`), `:1652-1666` (`RetryLoopTerminal`).
- ADR: [0044](0044-overflow-recovery-ladder.md) Decision 4 (measure refined here); [0048](0048-compaction-ratio-target-and-oversized-tail-order.md) §2 (implementation gap recorded here), §6 (scope note here).
