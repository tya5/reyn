# ADR-0045 (#5592) — spill granularity: one request per (compartment × spillability), not per turn

**Status**: **ACCEPTED** (owner ruling 2026-08-30; raised from PROPOSED on 2026-09-02 after [#5596](https://github.com/tya5/reyn/pull/5596) landed and each decision below was checked against `origin/main` at `5a9cb2aad` — see *Verification*).
**Supersedes**: [ADR-0044](0044-overflow-recovery-ladder.md) — **its rung-① granularity only** ("Rung 1 spills **one** candidate and retries rather than spilling a batch"). Every other part of 0044 — the classification, the cause-independent ladder, the predicate terminals, the slice-sizing search, `Spillability` itself — stands unchanged.
**Track**: #5531 → #5592.

## Context

ADR-0044 ruled that rung ① spills **one** candidate and retries, on the reasoning that "the next call is the cheapest way to learn whether enough has gone". That reasoning holds only while the candidate count is small.

On the owner's machine (2026-08-30), one incoming message produced **43 compaction LLM calls in 4m19s**, returned no reply, and ended when the owner killed the client. No terminal event was emitted, because none was reached: the loop was not stuck, it was **bounded by the candidate count** — large enough on that history to project to roughly four hours of continuous upstream calls at the observed rate. (The exact candidate count itself was never measured that night — the audit trail carried `compaction_started.new_turn_count`, the turn count offered to `compact()`, not a spillable-candidate count; this PR's own observability fields close that specific gap going forward.)

Three facts make that shape unaffordable rather than merely slow:

- Each rung-① retry is a **full-payload upstream request**, not a cheap probe.
- A request the upstream **rejects is still billed** — the owner observed their subscription budget actually drop.
- The cost is **not observable from inside reyn**: the only exact quantity available is the number of requests sent. Wire bytes are a disclosed under-measurement (they exclude tool-schema bytes; that call carried 65 tools), and `prompt_tokens`/`cost_usd` on that path are `usage_source: "estimated"`.

ADR-0044's termination argument was a **well-founded measure** — a proof that the loop ends. It was used as though it were also a bound on what the loop costs. Those are different claims, and the charter separates them: cost/budget (bounding) is its own band member, distinct from crash-recovery.

## Decision

**Within a spill stage, one request carries every candidate of the same `Spillability` in that compartment.**

- The unit is **(compartment × `Spillability`)**: `head`'s `FIRST_CHOICE` in one request, `tail`'s `FIRST_CHOICE` in another. `head` and `tail` are **not** merged into one request.
- This applies at **both** entry points — the `main_call` overflow and the `compact()` overflow nested inside it.
- The **population is the range that request sends**: the slice actually offered to `compact()`, or the compartments `main_call` actually sends. (On the first attempt `_compact_attempt_len` is `None`, so the offered slice *is* all of `raw_middle`; the two readings only diverge after halving, and the offered slice is the one that governs.)
- **An empty population sends nothing** — the tier is skipped, never probed with a request that carries no spill.
- `LAST_RESORT` is reached only when `FIRST_CHOICE` is exhausted, unchanged from 0044.

**The previous behaviour stays available as configuration**, defaulting to the batched form:
`chat.compaction.spill_granularity` takes `tier` (default — one request carries a whole
`Spillability` tier) or `turn` (one spill-out per request). Both values are units, and the setting
is exactly that choice — deliberately not a numeric batch size, which would add a knob nobody can
derive a correct value for. `tier`, not `spillability`, so the value never collides with
`Spillability` the type. `tier` names a `Spillability` tier here — never the `head`/`mid`/`tail`
positional compartment, which this field does not select (recorded as a disagreement, not settled
by consensus: architect's own reading is that `tier` risks being misread as the positional stage,
now that `main_call`'s own head/tail compartments are in scope alongside `compact()`'s; the
implementer's call was to keep `spill_granularity`/`tier`, already implemented and tested, and add
this clarifying line rather than rename). `turn` belongs next to a line saying it trades requests
for over-spill and is not the safe side.

## Consequences

- Rung ①'s request count stops being proportional to the candidate count. The incident's shape — bounded, but bounded by the size of the history — cannot recur through this rung.
- Spilling a whole tier spills more than strictly needed. What that costs is **read-back**, not loss: the content is on disk and reachable. Under overflow the conversation is already degraded, and the alternative was paying for one full request per turn.
- Batching a tier of very small turns can enlarge the payload rather than shrink it, since a preview carries fixed overhead. This is **left unaddressed by design** (owner ruling): the `T_max` halving already keeps the number of such rounds small.
- Selecting `one spill-out at a time` re-enables the incident's shape. That is a legitimate choice — it minimises over-spill — but it is **not the safe side**, and the configuration's documentation says so.
- Nothing else moves: the population's definition, the `FIRST_CHOICE` → `LAST_RESORT` order, `NEVER`'s exclusion, what a single spill does to one turn, the ladder's rung order, the terminals, the episode scoping, and the definition of progress ("a candidate was consumed" — consuming many at once does not change the definition).

## Verification against the merged implementation

Checked on `origin/main` at `5a9cb2aad` (the check is stamped with the tree it ran
against, so a reader can tell when it was overtaken). Each decision is paired with the
test in #5596 that goes red without it — the witness, not the PR body's account.

| decision | witness on `main` |
|---|---|
| One request per (compartment × `Spillability`) | `test_tier_granularity_spills_whole_mid_tier_in_one_call`, `test_tier_batch_consumes_15_candidates_in_2_compact_calls` |
| `head` and `tail` are never merged into one request | `test_head_and_tail_batches_never_merge_into_one_spill_call` |
| An empty population sends nothing | `test_spill_fn_returning_empty_list_falls_through_to_halving` (`router_loop_driver.py`: a tier with zero eligible candidates is skipped without a call) |
| The per-turn form stays available, not as default | `chat.py`: `spill_granularity: Literal["tier", "turn"] = "tier"`; `test_spill_granularity_turn_reproduces_one_candidate_per_call` |
| The request count is the one exact cost quantity | `test_upstream_recovery_call_count_increments_once_per_real_call` — the count this ADR's context said nobody was recording is now a field |

Nothing was falsified. The incident-size figure the first draft carried was replaced
before merge by the implementer's own measured count, so no unverified number
remains in this record.

## References

- Issues: [#5592](https://github.com/tya5/reyn/issues/5592) (the incident and this ruling), [#5531](https://github.com/tya5/reyn/issues/5531) (the ladder), [#5514](https://github.com/tya5/reyn/issues/5514) (`Spillability`), [#5588](https://github.com/tya5/reyn/issues/5588) (making this visible — separate scope).
- ADRs: [0044](0044-overflow-recovery-ladder.md) (superseded in rung ① only), [0042](0042-force-close-layer2-removal.md).
