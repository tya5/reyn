# ADR-0044 (#5531) — overflow recovery: one cause-independent ladder, predicate terminals, declared spillability

**Status**: ACCEPTED (owner design session 2026-08-30). **Implementation**: #5531 PR-3 (with [#5543](https://github.com/tya5/reyn/issues/5543) and [#5514](https://github.com/tya5/reyn/issues/5514) §8 in the same PR, owner ruling: 「同じ PR でいれて」).
**Track**: #4381 (overflow recovery) → #5367 → #5531.
**Builds on**: [ADR-0042](0042-force-close-layer2-removal.md) — spill replaced force-close layer②. This ADR keeps that decision and describes the ladder spill now sits at the top of. Nothing in 0042 or [ADR-0036](0036-history-compaction-force-close-unification.md) is superseded.
**Companion docs**: [`chat-compaction.md` → Overflow recovery](../../concepts/data-retrieval/chat-compaction.md#overflow-recovery) describes the resulting mechanism; this ADR records why.

## Context

`retry_loop` recovers an overflowed request by shrinking and retrying. Four
properties of the shipped loop were found to be accidents of how it grew rather
than decisions anyone made:

1. **Spill — the only rung that removes content rather than moving it — sat last**, reachable
   only at two doorsteps immediately before `UnrecoveredError`, limited to `raw_middle[0]`,
   and gated on `role == "tool"`. Every earlier rung only relocates content between
   `head`/`mid`/`tail`, so the loop exhausted its relocation levers before trying the one
   that shrinks.
2. **Two rungs were gated on the failure being an HTTP 413**, so a token-shaped cause could
   not reach the `T_max` halving at all — the same-cause cap raised first.
3. **`max_iterations` bounded the loop.** It answers "we tried enough times", which is not
   the claim "nothing is left to try".
4. **Every `compact()` exception except quota exhaustion was wrapped as an overflow**, so a
   5xx or an `AttributeError` was answered by shrinking. A successful shrink folds turns into
   a summary irreversibly, so the wrong remedy also degrades the history.

## Decision

### 1. Classification precedes the ladder

Failures are classified into `Overflow` / `Retryable` / `Fatal` — the shape #3783 already
proposed and whose `Retryable` and `Fatal` arms never shipped. Only `Overflow` enters the
ladder. `Retryable` is retried with backoff (the compaction call does not currently go
through the router's own retry wrapper — a 5xx reaching the ladder has not been retried even
once). `Fatal` propagates unchanged.

This generalises #5329, which carved out exactly one subtype (quota exhaustion) for exactly
this reason: the window "resets on a clock, not on input size".

### 2. Byte limits and token limits take the same rungs

A 413 and a context-window rejection are observed differently and **reported** differently —
`UnrecoveredError.saw_byte_limit` and the operator-facing message keep the distinction, and
`router_loop_driver` branches on that field *after* the terminal. Inside the ladder they are
one cause. No reason was found for the split: each existing guard states why a rung is
*needed* for 413, never why it is *forbidden* for a token cause.

The same-cause consecutive cap is retired with the guards. "This cause recurred" is not
evidence that no lever is left while a lever remains untried.

### 3. Spill is the first rung, one candidate at a time

On an overflow, spill one candidate and retry the same call; repeat until the overflow
clears; fall through only when no un-spilled candidate is left.

Candidates come from **the payload that was rejected** — `raw_middle` for a `compact()`
overflow, `head` + `tail` for a `main_call` overflow. `mid` is never on the wire, so a turn
left there is compacted or the call fails; that is also why the mid floor has no
defer-to-tail escape (its removal is part of this decision).

Because a spilled turn is persistent, a candidate outside the slice currently offered to
`compact()` still shrinks a later fold's input. A spill may therefore leave *this* attempt
unchanged, so progress is defined as **"a candidate was consumed"**, never "the wire got
smaller" — reading bytes here reintroduces the confusion #5367③ named.

### 4. Terminals are predicates; `max_iterations` is removed

Two terminals remain: mid is one turn that cannot be made smaller, and the room halving
reached its floor (`SP` plus the newest message alone no longer fit). Termination rests on
two nested measures — within an episode, un-spilled candidates and `len(head) + len(tail)`
strictly decrease and the halving is bounded by its own floor; across episodes, the total
turn count strictly decreases, because returning to `main_call` requires a successful
`compact()`.

`max_iterations` is removed **in the same change as the classification, never before it**:
without `Fatal`, an `AttributeError` in our own code would shrink the entire history away
before surfacing.

### 5. Slice sizing halves on failure and doubles on success, episode-scoped

One-way halving was measured (#4950) against a uniformly hard-to-compact input, where
resetting to full after every success re-discovered the same size each round. It mispredicts
the opposite shape: halving to one succeeded *because* that turn dominated, and once it is
folded the remainder may compact in one call. Both directions serve one shape each. The value
belongs to one overflow episode — returning to `main_call` proves the whole is smaller, so the
size that failed describes a history that no longer exists.

### 6. What may be spilled is declared, not sniffed (`Spillability`)

Ordering within `mid` is not a size question. `mid` is the summariser's input, so spilling a
turn removes its content from the summary that permanently replaces it. Spilling an
instruction and spilling a tool result are therefore not equivalent: the summariser exists to
fold evidence and keep decisions.

`Spillability` — `FIRST_CHOICE` / `LAST_RESORT` / `NEVER`, default `LAST_RESORT` — is declared
by the producer at each `_append_history` call site. `NEVER` means "spilling this makes the
rest false", not "this is important": a state-change notice that disappears returns the model
to the #352 refusal-trap pattern its own docstring names.

`role` cannot express this: a peer injection's role is not even a literal, reyn's own frame
notices and its injected hook material are both `role == "system"`, and hand-typed and pasted
user input are both `role == "user"`.

The tier is a **predicate, not a cursor** — recomputed from the current population on every
selection, so `FIRST_CHOICE` returns by itself when a refill brings new turns into `mid`.

`NEVER` governs spill only; a summary marked `NEVER` is still folded and replaced, per #5531's
own invariant.

### 7. Hook-authored content declares its own spillability

`template_push` and `exec_capture` write history whose nature reyn cannot know — a template
injecting the current git branch is material, one injecting a standing policy is frame. Both
take a `spillability:` key (default `FIRST_CHOICE`, because unbounded hook pushes are the
problem #5514 was filed for); `exec` takes none, because its output is ignored.

`spillability: NEVER` is rejected at the agent-writable layers (`per-agent` / `per-session`),
reusing the existing self-grant boundary (#5356) — an agent declaring its own content
unspillable is a claim on the window nobody authorised. Declaring `NEVER` also requires
declaring a size cap: content that cannot be spilled must be bounded on the way in, or no
subject bounds it at all.

## Alternatives considered and rejected

| considered | rejected because |
|---|---|
| A new terminal for "the ladder is exhausted" | The room halving lowers the floors, so the exhausted state is not permanent — the rung refills `mid` on the next pass. |
| Keep the mid floor's defer-to-tail for non-byte causes | It was the only rung that grew `tail` and the only one that reset the slice size, i.e. the sole cycle in the loop. Classification removes the failures it protected instead. |
| Order `mid` spill by `role == "tool"` | Puts a wire-level term in a core ordering expression, so every new content kind edits it — and it cannot express the group that matters (peer instructions). |
| An enum on "does this content have a recovery mechanism" | Deferred, not rejected. It decides eligibility for a `drop` operation that does not exist; with GC out of scope and spill preserving content, it has no consumer today. Revisit when `drop` is implemented, GC enters scope, or an op declares itself idempotent. |
| Spill a batch at the more fatal entry point | The next call is the cheapest way to learn whether enough has gone; the missing aggressiveness was the role predicate, not the quantity. |

## Consequences

- Recovery becomes reproducible from the history alone: the same content under the same
  pressure shrinks the same way, because no rung consults the cause shape and no state
  survives an episode.
- Every overflow episode pays a fresh slice-size search. This is accepted, and named here so
  it is not discovered as a surprise.
- `NEVER` is a claim on irreducible window space. Its only mechanical bound is the size cap
  required alongside it; a future member added to `NEVER` without one would reintroduce the
  unbounded-growth shape.
- Naming note (see this directory's README): this ADR **decides** `Spillability` and its
  members, so a later rename is an ADR decision rather than a sweep.

## References

- Issues: [#5531](https://github.com/tya5/reyn/issues/5531) (flow), [#5514](https://github.com/tya5/reyn/issues/5514) (`Spillability`), [#5543](https://github.com/tya5/reyn/issues/5543) (classification), #3783 (`classify_llm_failure`), #5329 (quota carve-out), #5364 / #5367 (spill staging and the byte-reading confusion), #4950 (slice-size measurement), #352 (refusal trap), #5356 (self-grant boundary).
- Docs: [`chat-compaction.md` → Overflow recovery](../../concepts/data-retrieval/chat-compaction.md#overflow-recovery).
- Code: `src/reyn/services/compaction/engine.py` (`retry_loop`), `src/reyn/runtime/services/router_loop_driver.py` (`_spill_candidates`, `_spill_fn`), `src/reyn/hooks/loader.py` (the agent-writable boundary).
