# ADR-0045 (#5592) — spill granularity: one request per (compartment × spillability), not per turn

**Status**: **PROPOSED** (owner ruling 2026-08-30, after a live incident on the owner's machine). Not ACCEPTED yet, by this team's own convention: an accepted ADR is immutable, so it is raised once the implementation has confirmed or falsified each decision below — the precedent ADR-0044 followed.
**Supersedes**: [ADR-0044](0044-overflow-recovery-ladder.md) — **its rung-① granularity only** ("Rung 1 spills **one** candidate and retries rather than spilling a batch"). Every other part of 0044 — the classification, the cause-independent ladder, the predicate terminals, the slice-sizing search, `Spillability` itself — stands unchanged.
**Track**: #5531 → #5592.

## Context

ADR-0044 ruled that rung ① spills **one** candidate and retries, on the reasoning that "the next call is the cheapest way to learn whether enough has gone". That reasoning holds only while the candidate count is small.

On the owner's machine (2026-08-30), one incoming message produced **43 compaction LLM calls in 4m19s**, returned no reply, and ended when the owner killed the client. No terminal event was emitted, because none was reached: the loop was not stuck, it was **bounded by the candidate count** — 2469 on that history, i.e. roughly four hours of continuous upstream calls.

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
`chat.compaction.spill_per_request` takes `spillability` (default — one request carries a whole
tier) or `turn` (one spill-out per request). Both values are units, and the setting is exactly that
choice — deliberately not a numeric batch size, which would add a knob nobody can derive a correct
value for. `turn` belongs next to a line saying it trades requests for over-spill and is not the
safe side.

## Consequences

- Rung ①'s request count stops being proportional to the candidate count. The incident's shape — bounded, but bounded by the size of the history — cannot recur through this rung.
- Spilling a whole tier spills more than strictly needed. What that costs is **read-back**, not loss: the content is on disk and reachable. Under overflow the conversation is already degraded, and the alternative was paying for one full request per turn.
- Batching a tier of very small turns can enlarge the payload rather than shrink it, since a preview carries fixed overhead. This is **left unaddressed by design** (owner ruling): the `T_max` halving already keeps the number of such rounds small.
- Selecting `one spill-out at a time` re-enables the incident's shape. That is a legitimate choice — it minimises over-spill — but it is **not the safe side**, and the configuration's documentation says so.
- Nothing else moves: the population's definition, the `FIRST_CHOICE` → `LAST_RESORT` order, `NEVER`'s exclusion, what a single spill does to one turn, the ladder's rung order, the terminals, the episode scoping, and the definition of progress ("a candidate was consumed" — consuming many at once does not change the definition).

## References

- Issues: [#5592](https://github.com/tya5/reyn/issues/5592) (the incident and this ruling), [#5531](https://github.com/tya5/reyn/issues/5531) (the ladder), [#5514](https://github.com/tya5/reyn/issues/5514) (`Spillability`), [#5588](https://github.com/tya5/reyn/issues/5588) (making this visible — separate scope).
- ADRs: [0044](0044-overflow-recovery-ladder.md) (superseded in rung ① only), [0042](0042-force-close-layer2-removal.md).
