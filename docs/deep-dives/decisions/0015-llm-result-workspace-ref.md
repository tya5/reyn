# ADR-0015: LLM result workspace ref threshold

**Status**: Accepted (2026-05-04)
**Track**: R-D10 (commit `20bf16d`)

## Context

[ADR-0004](0004-memoization-key-design.md) records LLM call results as
inline JSON in `step_completed` events. A typical LLM response is 5 –
30 KB; a complex Control IR with embedded reasoning can reach 50 KB+.

Recorded inline in WAL events, those payloads:

- Get serialised on every WAL append (= I/O cost on the hot path).
- Stay in memory during AgentRegistry replay (= memory cost on
  resume).
- Resist truncation: per-phase truncation
  ([ADR-0001](0001-state-model-wal-snapshot.md)) keeps the directly
  in-flight phase's events around, and one phase with several act_turns
  can carry MB of payload that the active session can't drop.

The right shape was to off-load large payloads to files and reference
them from the WAL event, mirroring how the workspace already holds
artifact bodies for data passed between phases (P5).

## Considered alternatives

- **A. Always inline, never ref.** Status quo; defers the problem to
  WAL truncation, which we've already shown can't reach the in-flight
  phase's events.
- **B. Always ref, never inline.** Extra file per LLM call regardless
  of size. Filesystem overhead for small payloads is wasteful;
  thousands of tiny files for short conversations.
- **C. Threshold-based: inline if small, ref if large.** Pick a
  threshold that captures the bloating cases (large reasoning output)
  while keeping the common path inline.

## Decision

**Adopt C with 32 KB threshold.** New module `src/reyn/skill/llm_result_ref.py`:

- `write_if_large(result, *, run_id, agent_dir, threshold=32 * 1024)`
  serialises `result`; if over threshold, writes
  `<agent>/skills/<run_id>/llm_results/<seq>.json` and returns
  `{"_ref": "<path>"}`. Otherwise returns the inline result unchanged.
- `resolve(result_or_ref, *, agent_dir)` is the symmetric read: if
  the dict has `_ref`, load the file; else pass through.
- `cleanup_for_run(*, run_id, agent_dir)` deletes the per-run
  directory; called from `SkillRegistry.complete`.

Wired in `OSRuntime._call_llm_and_record`:

- Before WAL emit: `result = write_if_large(result, ...)`.
- On memo hit: `recorded = resolve(recorded, ...)` before returning to
  the caller.

The 32 KB threshold was picked to leave typical short-form LLM
responses inline while moving large reasoning / Control IR payloads to
disk. No tunable knob; a code-level constant.

## Consequences

**Positive:**

- WAL events for large LLM calls shrink from KB-MB to ≈ 100 bytes (the
  ref dict).
- Memo hit path is transparent: resolve happens inside the runtime
  helper, callers see identical shape to the inline case.
- Lifecycle is bound to `SkillRegistry.complete`, so finished runs
  release their result files automatically. Resume-preserving paths
  ([ADR-0013](0013-exception-aware-crash-lifecycle.md)) keep the
  files until the next completion.
- Files live under the per-skill workspace directory, so the
  workspace's existing permission model (P5) covers them.

**Negative:**

- One extra file open per memo hit on large results. Negligible
  compared to the LLM call cost being avoided.
- Recorded `_ref` paths are absolute relative to `agent_dir`, which
  must remain stable across restarts. If `agent_dir` moves, refs
  break. Same constraint as workspace artifacts; documented.
- 32 KB is a magic number. Code comment + this ADR are the canonical
  source.

**Precluded:**

- Generic large-payload off-loading for `step_completed` of arbitrary
  ops. The pattern is general enough to extend if needed (e.g.
  large MCP read results); R-D10 scoped to LLM only.

## References

- Commit `20bf16d` — implementation + Tier 2 / 3 tests
- [ADR-0001](0001-state-model-wal-snapshot.md) — WAL + truncation
- [ADR-0004](0004-memoization-key-design.md) — memoization key (the
  contract this extends transparently)
- [ADR-0013](0013-exception-aware-crash-lifecycle.md) — `complete()`
  hook used for cleanup
