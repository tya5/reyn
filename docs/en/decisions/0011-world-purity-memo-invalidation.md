# ADR-0011: World-purity memo invalidation on resume

**Status**: Accepted (2026-05-04)
**Track**: PR-memo-purity-fix (commit `7e764ce`)

## Context

[ADR-0003](0003-op-purity-classification.md) classified ops into five
purity tiers (`pure / world / side_effect / external / llm`). The memo
contract from [ADR-0004](0004-memoization-key-design.md) returned a
recorded result for any matching `(op_invocation_id, phase, args_hash)`
key, regardless of purity.

Dogfood scenarios surfaced a failure mode:

```
act_turn 1: mcp/search("foo") → API throttled → 0 results memoised
crash → resume
act_turn 1 (resumed): memo hit → "0 results" forever
                       → LLM concludes "nothing to find"
                       → skill loops on the same false negative
```

The skill author can't fix this from inside the skill — there is no way
to express "this op's result is only valid in this run". Re-execution
across runs is what `world` purity means semantically (= the result
depends on external state at call time), so the memo replay is the
defect.

The same shape exists for `file/read` (file changed under us between
crash and resume) and any read-only MCP API.

## Considered alternatives

- **A. Invalidate world memos at run boundary.** On resume, treat all
  `world` purity step_completed events as if they didn't exist; ops
  re-execute and emit fresh step_completed. Within a single run, world
  memos still hit (= same act_turn re-entry replays consistently).
- **B. TTL on memo entries.** Add a wall-clock stamp; memos older than
  N seconds invalidate. Hard to pick N; doesn't capture the semantic
  ("crash boundary").
- **C. Skill-author opt-out per op.** Each `world` op declares whether
  it wants resume-stable replay or fresh execution. Burdens the skill
  author with a question they shouldn't have to answer; defeats the
  purity classification's purpose.
- **D. Drop memo for all ops on resume.** Loses LLM cost protection
  (the headline value of memoization).

## Decision

**Adopt A: world purity invalidates across run boundary, holds within a
run.**

Implementation in `src/reyn/dispatch/dispatcher.py`:

```python
memo = (
    None
    if purity is OpPurity.world
    else _lookup_memoized_step(
        ctx.resume_plan, op_invocation_id, ctx.phase, args_hash,
    )
)
```

`ctx.resume_plan` is non-None only on a resume run, so `world` ops
behave normally during the original run (= memo recorded for in-run
re-entry consistency). On resume the `is None` branch forces fresh
execution.

`pure` ops never memoise (covered by ADR-0003); `side_effect / external
/ llm` ops keep full memo replay (covered by ADR-0004). Only `world`
ops change.

## Consequences

**Positive:**

- Flaky read APIs (rate limit, transient zero-result) self-heal on
  resume rather than locking the skill into a stale answer.
- No skill-author burden — purity classification already encoded the
  right semantic; this just makes the memo path honour it.
- LLM cost protection unchanged (LLM purity untouched).
- Consistent within a run: if `world` op X is invoked twice in the same
  act_turn, the second hit still replays from memo (= same as before).

**Negative:**

- Resume re-executes all `world` ops from the in-flight phase. Cost
  delta is bounded — typically < 100 ms per op, no LLM tokens consumed.
  Worst case for a phase with many file reads: a few seconds. Trade-off
  against the alternative (= skill stuck on stale data) is heavily in
  favour of re-execution.
- Possible behavioural drift: if the file / MCP state changed between
  original run and resume, the resumed phase sees the new state. This
  is the *intended* semantic ("world purity = result depends on world
  state at call time") but worth noting in the upgrade docs.

**Precluded:**

- Bit-perfect resume of a `world`-heavy phase across runs. Not a goal
  given the failure mode this resolves.

## References

- Commit `7e764ce` — implementation + Tier 2 tests
- [ADR-0003](0003-op-purity-classification.md) — purity classification
- [ADR-0004](0004-memoization-key-design.md) — memoization key
- [ADR-0012](0012-auto-resume-default.md) — auto-resume default which
  this fix unblocked
