# ADR-0042 (#4381) — force-close layer② removal: spill replaces the consolidate-and-retry path

**Status**: ACCEPTED + IMPLEMENTED (2026-08-12, landed in [#4454](https://github.com/tya5/reyn/pull/4454)).
**Supersedes**: [ADR-0036](0036-history-compaction-force-close-unification.md) — **its "Implementation note: PR-F2b force-close handoff cap" section ONLY**. Every other part of 0036 (the within-unit history convergence, the shared `CompactionEngine`, FD1–FD7) stands unchanged.
**Track**: #4381 (overflow recovery), PR-4.

## Decision

Owner ruling, verbatim (2026-08-12):

> **２の force close 廃止して spill にしよう。予算のための force close は残すで良い**

## 🔴 Scope boundary — there are TWO force-closes, and only one is removed

This is the single most important line in this ADR, because the two share a name and a
`grep` for `force.close` returns both.

| | what it bounds | axis | fate |
|---|---|---|---|
| **layer① — turn-budget force-close** (`force_close_triggered`) | cumulative **cost/budget** for the turn | cost | ✅ **UNCHANGED, still live** |
| **layer② — driver force-close handoff** (`router_force_close_handoff`) | **context size** overflow after bounded shrink | context size | ❌ **removed** |

**layer① remains implemented and is not affected by this decision.** Its live surfaces:

```
src/reyn/prompt/turn_budget.py             the wrap-up system prompt (T_wrap_SP)
src/reyn/core/events/event_schema.py:256   "force_close_triggered" (a declared audit-event kind)
src/reyn/llm/llm.py:2765                   the tools=[] path used by the wrap-up call
src/reyn/observability/otel_exporter.py:117  limit_denied — "a force-close is imminent"
```

∴ **doc prose describing "bounded loops with graceful force-close" (charter, `agent-engineering/reliability-engineering.md`, `system-design.md`) is CORRECT and must not be purged.** It describes layer①. A drift-purge pass that greps `force-close` and deletes the hits would remove the description of a live mechanism. (This ADR exists partly to make that mistake expensive to make: the architect's own first pass on #4454 read those 11 doc hits as residue before checking `src/`.)

## What was removed

```
_force_close_handoff / _force_close_wrap_up        runtime/services/router_loop_driver.py
_is_force_close_consolidation                      the durable F2a covers-respecting reset
                                                   in RouterHistoryBuffer.build_history
_MAX_FORCE_CLOSE_HANDOFFS                          both declarations (router_loop_driver.py
                                                   and the orphan duplicate in session.py)
```

## Material that informed the ruling (evidence, not the decision itself)

⚠️ **Recorded as READ, not measured by this ADR's author** — the figures below are quoted from
`router_loop_driver.py`'s own pre-removal docstring (`:305-320`):

- The handoff **fired 0 times in real use** — after 61 apparent firings turned out to be
  test-fixture contamination.
- When it did fire, the F2a consolidation caused **permanent loss of `head`**.

∴ the mechanism cost a real, irreversible loss and delivered no observed recovery. **The decision
is the owner's; this material is why it was put in front of them.**

## Consequence — what the system does now

An overflow that survives `_run_with_shrink`'s own bounded shrink attempts is **unrecovered on the
first attempt**; there is no consolidating retry. What keeps an oversized single result from
reaching that point at all is the **spill** family (#4381, e.g. [#4432](https://github.com/tya5/reyn/pull/4432)):
a tool result that would not fit is written to disk and replaced by a reference, with a working
resume position, rather than being consolidated after the fact.

**This is a deliberate trade**: the removed mechanism recovered *after* the overflow at the cost of
silently destroying `head`; spill prevents the overflow *before* it happens and destroys nothing.

## Open (not decided here)

- The **resource-bound / budget-bound unit separation** and the ordering invariant between the read
  cap and the spill trigger (#4381 PR-1, landed in `resolve_effective_trigger_and_budgets`) is a
  neighbouring decision, not this one.
- Whether `_run_with_shrink`'s own shrink ladder should change is untouched by this ADR.

## See also

- [ADR-0036](0036-history-compaction-force-close-unification.md) — the superseded section, kept as
  historical record of the removed mechanism's reasoning (its body carries a supersession note added
  by #4454; this ADR is the record the `decisions/README.md` immutability rule actually asks for).
- #4381 (umbrella), #1092 (0036's own umbrella).
