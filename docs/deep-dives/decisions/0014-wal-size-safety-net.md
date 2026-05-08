# ADR-0014: WAL size safety net trigger

**Status**: Accepted (2026-05-04)
**Track**: R-D4 (commit `9a561c1`)

## Context

WAL truncation in [ADR-0001](0001-state-model-wal-snapshot.md) had two
trigger surfaces:

1. **Semantic boundary**: `skill_phase_advanced` / `skill_completed`
   events queued an async `truncate_wal_if_eligible` call.
2. **5-second throttle**: subsequent triggers within 5 s skipped to
   avoid rewrite thrashing during phase bursts.

For a session that runs continuously (= phase advance events fire),
the floor moves and the WAL stays bounded. But two quiet patterns
broke that:

- A long-idle session (user opened chat, didn't send messages) —
  semantic events never fire, WAL grows from background events
  (interventions, chains, snapshots).
- A long-running skill awaiting `ask_user` — phase doesn't advance,
  semantic boundary trigger doesn't fire, idle inbox events accumulate.

Without a size-driven trigger, the WAL can grow unboundedly in those
cases and bloat the next replay.

## Considered alternatives

- **A. Periodic timer (every N seconds).** Fires regardless of
  activity. Wastes wakeups on a fully idle process; non-trivial
  interaction with the asyncio event loop's lifecycle.
- **B. Size-driven trigger at chat turn boundary.** When the user
  sends a message and `ChatSession._handle_user_message` runs, check
  WAL file size; if over threshold, force-truncate. Aligns with user
  activity points; no idle-loop overhead.
- **C. Fold size check into existing semantic trigger.** The semantic
  trigger never fires when the problem occurs (= idle / awaiting), so
  this fixes nothing.

## Decision

**Adopt B.** New mechanism in `src/reyn/chat/registry.py`:

```python
_SIZE_SAFETY_NET_BYTES: int = 1_000_000   # 1 MB

async def maybe_truncate_for_size(
    self, *, threshold_bytes: int = _SIZE_SAFETY_NET_BYTES,
) -> None:
    if (size := self._wal_size_bytes()) >= threshold_bytes:
        await self.truncate_wal_if_eligible(bypass_throttle=True)
```

`bypass_throttle=True` is the new code path; the regular semantic
trigger keeps the 5 s throttle. The size check is `os.stat`, cheap
enough to call on every chat turn.

Hooked into `ChatSession._handle_user_message` so the size check
fires on user activity. Long-idle sessions that never receive user
input simply never trigger — but they also produce no new WAL
content, so they don't grow either.

## Consequences

**Positive:**

- Long-await skills (ask_user mid-flight) no longer pin WAL growth.
- 1 MB threshold is a generous header for a single chat turn — typical
  WAL is < 100 KB across many turns.
- Throttle bypass is bounded to the size-safety path, so the existing
  rewrite-thrashing protection on the semantic path is preserved.

**Negative:**

- Doesn't fix the deeper "long-await skill pins floor" problem
  ([R-D16](#r-d16-followup) below). Size trigger still can't drop
  events the floor protects. This is a separate design question.
- `1_000_000` is a magic number. Defended by code comment + ADR; no
  user-facing setting yet (could be added if needed).

**Precluded:**

- Periodic timer-based truncation. Documented as out-of-scope for
  pre-1.0; the user-activity hook covers the realistic loads.

## R-D16 follow-up {#r-d16-followup}

External review surfaced that the size safety net cannot help if the
floor is pinned low by a single long-await skill. The floor is `min(
all_active_skill.last_phase_applied_seq) + 1`; one stuck skill at
seq=5000 blocks all entries 5001+ from being dropped, regardless of
size.

R-D16 (planned, not yet landed) will add a "wait-aware floor"
calculation that excludes skills awaiting user input beyond a
threshold. Tracked in the plan file. ADR will be written when R-D16
implementation lands.

## References

- Commit `9a561c1` — implementation + Tier 2 tests
  (`test_registry_wal_size_safety_net.py`)
- [ADR-0001](0001-state-model-wal-snapshot.md) — the WAL +
  floor-truncation model this extends
- R-D16 in the plan file — long-await pin follow-up
