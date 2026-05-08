# ADR-0001: WAL + snapshot cache (transactional event-sourced replay)

**Status**: Accepted (2026-05-02)
**Track**: PR-state-foundation, D-track

## Context

Reyn is an Agent OS where multi-phase skills run for minutes to hours.
A process crash mid-skill (kill -9, OOM, machine reboot) destroys the
asyncio task. PR21 added basic crash recovery for inbox / pending_chains
but skill execution itself was still ephemeral.

We needed a persistence model that:

1. Survives process crashes without losing committed work
2. Allows multi-skill, multi-agent state to coexist coherently
3. Has predictable size growth (long-running agents over weeks)
4. Supports concurrent writers (multiple skills + chat session)
5. Is simple enough to debug from disk content alone

## Considered alternatives

- **A. In-memory only** (PR21 baseline). Rejected: skill state vanishes
  on crash. Misses the entire goal.
- **B. Per-skill state files, no WAL.** Rejected: no global ordering;
  multi-skill / cross-agent operations can't be replayed coherently.
  Concurrent writers race on file rewrites.
- **C. Embedded SQL DB (SQLite).** Rejected as overkill: adds dep,
  query layer, schema migrations all to manage append-only logs that a
  jsonl file handles for free. Operator visibility worsens (binary file).
- **D. WAL + derivable snapshot cache.** Adopted. WAL is global jsonl,
  append-only, monotonic seq counter. Snapshots are derived from WAL
  replay; if a snapshot is corrupt or absent, replay from
  applied_seq=0 reconstructs it. WAL truncation is gated by the
  minimum applied_seq across all snapshots. (Same general pattern as
  database WAL recovery and journaling filesystems.)

## Decision

**Adopt D: WAL + snapshot derivable cache.**

Architecture:

- `.reyn/state/wal.jsonl` — global single file, single seq space.
  Append-only. Every state mutation is recorded.
- `.reyn/agents/<name>/state/snapshot.json` — per-agent snapshot cache,
  records `applied_seq` so replay can resume from there.
- `.reyn/agents/<name>/state/skills/<run_id>.snapshot.json` — per-skill
  snapshot cache, lifecycle bound to skill_started → skill_completed
  (or skill_discarded).

Hard invariants:

1. **WAL is a global single file with single seq space.** Per-agent /
   per-skill / per-process splits are explicitly forbidden — splitting
   loses cross-agent ordering (a well-known pitfall in any layered
   log hierarchy: the lower layer's truncation can drop entries that
   the higher layer still needs to replay).
2. **Snapshots are derivable cache.** Removing or corrupting a snapshot
   never destroys data; the next startup replays the WAL.
3. **Truth is in the WAL.** Any disagreement between snapshot and WAL
   resolves in favor of the WAL.

## Consequences

**Positive:**

- Crash recovery works for any state mutation (inbox, chains, skill
  runs, interventions, budget) once it's in the WAL.
- Snapshots can be wiped (`--reset`) without losing audit log.
- Concurrent writers sequence cleanly via the WAL's monotonic seq.
- Operator can `tail -f wal.jsonl` to watch state mutations live.

**Negative:**

- Two-step writes (WAL append + snapshot rewrite) means a partial
  crash can leave snapshot lagging WAL. Mitigated by `applied_seq`
  filter on replay (idempotent).
- WAL grows unbounded without truncation; truncation logic added
  later (see ADR-0006 schema version + R-D4 size safety net).
- Snapshot rewrite cost on every mutation (every WAL append rewrites
  the per-agent snapshot). Acceptable for current message volumes.

**Precluded:**

- Cannot support disconnected / sharded multi-process scenarios; WAL
  must be local. Documented as out-of-scope (multi-process scaling
  is in residuals, low priority).

## References

- Commit `445292f` (PR21) — initial inbox / chain WAL foundation
- D-track Part A–C (PR-step-events, PR-state-foundation) — extended WAL
  taxonomy with skill_*, step_*, intervention_* events
- [docs/en/concepts/events.md](../concepts/events.md) — event log + WAL
  architecture
- [docs/en/concepts/skill-resume.md](../concepts/skill-resume.md) —
  user-facing summary
