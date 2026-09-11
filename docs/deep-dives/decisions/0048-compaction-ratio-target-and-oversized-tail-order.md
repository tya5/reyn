# ADR-0048 (#5890) — a compaction ratio target is a recovery-time goal, not an invariant; an oversized tail turn spills before it folds

**Status**: **ACCEPTED** (owner ruling, 2026-09-12, verbatim「ADR-0048 accepted にしていいよ」; raised from PROPOSED). Owner design session, [#5890](https://github.com/tya5/reyn/issues/5890) (verbatim quotes below); lead-coder ruling on the two invariant-scope decisions.
**Builds on**: [ADR-0044](0044-overflow-recovery-ladder.md) — the ladder this ADR's decisions apply inside. Nothing in 0044 is superseded.
**Track**: #5890.

## Context

Owner report (2026-09-07, chat, verbatim): 「reyn-self default compacting 頻発してる。一回の圧縮率が低すぎるんじゃないの」.

This ADR does not decide the open half of #5890 — how many rungs a fold-ratio gate needs, which sites it closes, its population, or how `refill` is implemented. Those are still being measured and remain open. What follows is only the part #5890 itself settled: what the operator expects a compaction outcome to look like, when that expectation applies, and one ordering decision for a specific case it names.

## Decision

### 1. The ratio expectation

Owner, verbatim:

> 私なら head/body/tail それぞれが config で指定された比率以下になることを目指すかな

> ユーザが compaction に求めるのは config で設定した比率になることでしょ

> 比率ぴったりに一致させることは不可能なので、どこまで許容するかを決める必要あるのかもね

The operator's expectation for a compaction outcome: each of `head`/`body`/`tail` ends up **at or below** its configured weight ratio. An exact match to the ratio is not expected — owner states it directly above.

### 2. Ordering for an oversized single tail turn

Owner, verbatim:

> tail が巨大な 1turn なのであれば spill して縮小、それでも大きいなら mid に移して compact が期待だよね。ただし、以前問題になった、mid/tail 間の相互移動の無限ループが解消できないと成立しないね。

When `tail` is dominated by one oversized turn, the order is: spill it to shrink it; if it is still too large after spilling, move it into `mid` so `compact()` can fold it. **Owner attaches this as a condition on the SAME sentence, not a separate note**: this ordering only holds if the mid/tail mutual-move infinite loop (a previously-known problem) is resolved — the ordering is not stated as unconditionally safe.

ADR-0044 already excludes this loop by making `mid` a sink (its own wording: "moving a turn out of `mid` without folding it is not a recovery step") — the condition owner attached appears already met by that decision, not by anything this ADR adds.

### 3. Over-shrinking is rejected

Owner, verbatim:

> 以前過剰縮小という問題もあったので、そうならないようにも気をつけるんだよ

The ratio expectation (decision 1) does not license over-shrinking — owner's own reason is a real prior incident (over-shrinking has happened before), not a hypothetical.

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
