# ADR-0048 (#5890) — a compaction ratio target is a recovery-time goal, not an invariant; an oversized tail turn spills before it folds

**Status**: **PROPOSED**. Owner design session, [#5890](https://github.com/tya5/reyn/issues/5890) (verbatim quotes below); lead-coder ruling on the two invariant-scope decisions. Raising to **ACCEPTED** is the owner's own act, not taken here.
**Builds on**: [ADR-0044](0044-overflow-recovery-ladder.md) — the ladder this ADR's decisions apply inside. Nothing in 0044 is superseded.
**Track**: #5890.

## Context

Owner report (2026-09-07, chat, verbatim): 「reyn-self default compacting 頻発してる。一回の圧縮率が低すぎるんじゃないの」.

This ADR does not decide the open half of #5890 — how many rungs a fold-ratio gate needs, which sites it closes, its population, or how `refill` is implemented. Those are still being measured and remain open. What follows is only the part #5890 itself settled: what the operator expects a compaction outcome to look like, when that expectation applies, and one ordering decision for a specific case it names.

## Decision

### 1. The ratio expectation

The operator's expectation for a compaction outcome: each of `head`/`body`/`tail` ends up **at or below** its configured weight ratio. An exact match to the ratio is not expected — owner has already stated a precise 1:1 match is impossible.

### 2. Ordering for an oversized single tail turn

When `tail` is dominated by one oversized turn, the order is:

1. **Spill it, to shrink it.**
2. **If it is still too large after spilling, move it into `mid` so `compact()` can fold it.**

Spill is tried first, before a move into `mid`, for this specific case.

### 3. Over-shrinking is rejected

The ratio expectation (decision 1) does not license over-shrinking. The fastest way to satisfy "at or below the ratio" is to discard everything — that implementation is explicitly rejected. A compaction outcome that meets decision 1 by emptying a compartment further than the ratio requires is not an acceptable way to reach it.

### 4. The ratio expectation is a recovery-time goal, not an always-true invariant

Decision 1 is **not** a property every compaction outcome must always satisfy. It is the **goal** a recovery pass pursues when recovery is already running — a target for `retry_loop` (ADR-0044) to aim at, not a standing constraint checked outside of it.

### 5. A ratio target must never turn a working run into a failure outside recovery

Outside `retry_loop`, nothing may move or shrink a turn in order to satisfy decision 1. Concretely: `MID_FLOOR` raises `UnrecoveredError` — it is a **recovery-path** terminal (ADR-0044 §4). Reusing it, or an equivalent forcing move, to chase the ratio target on a call that was not overflowing would convert an otherwise-succeeding run into a failure for a goal that only applies inside recovery.

### 6. Inside recovery, `MID_FLOOR` is the normal terminal for "ratio not reached"

When `retry_loop` is already running (an overflow is being recovered from) and the ratio target from decision 1 is not reached, hitting `MID_FLOOR` (ADR-0044's own terminal — mid is one turn that cannot be made smaller, or the room-halving floor) is the **expected, normal** way that recovery pass ends without meeting the target — not a separate failure mode this ADR introduces.

## Not decided here (explicitly out of scope)

The following remain open, under active measurement, and are deliberately not recorded as "TBD" placeholders — an ADR records decisions, not open slots:

- How many rungs/types a fold-ratio gate needs, and which sites it closes.
- The gate's population and predicate.
- `refill`'s implementation, and whether/how it adds a measurement item.

A future decision on any of these is either a new ADR or a revision of this one — never a silent addition to the code alone.

## References

- Issue: [#5890](https://github.com/tya5/reyn/issues/5890) — owner's verbatim report, the census that grounds decisions 1–3, and lead-coder's ruling grounding decisions 4–6.
- ADR: [0044](0044-overflow-recovery-ladder.md) — `retry_loop`, `MID_FLOOR`, and the terminal-predicate design decisions 4–6 apply inside.
