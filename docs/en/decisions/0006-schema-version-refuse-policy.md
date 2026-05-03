# ADR-0006: Schema version refuse + --reset (pre-1.0 policy)

**Status**: Accepted (2026-05-03)
**Track**: PR-resume-ux β U4

## Context

Reyn is pre-1.0. Snapshot schemas (per-agent + per-skill) and the WAL
format are still evolving. When a release introduces a breaking schema
change, what should the new code do with snapshots written by the old
code?

Three failure modes are possible:

1. **Silent corruption** — load anyway, ignore unknown fields, drop
   fields that no longer exist. The state is "almost" loaded, but
   subtle fields are lost. Bugs surface mysteriously days later.
2. **Hard crash** — load fails with a Python exception. User sees a
   stack trace, doesn't know what to do.
3. **Refuse + remediation** — load fails with a clear, actionable
   error. User runs the documented remediation command.

We needed to commit to one of these for pre-1.0 and define a future
path for post-1.0.

## Considered alternatives

- **A. Silent corruption.** Rejected: violates Reyn's predictability-
  first vision (memory: `project_reyn_vision.md`). Hard-to-debug
  bugs are the worst kind for an enterprise-grade tool.
- **B. Migrate-or-die.** Implement migration logic for every schema
  bump. Rejected for pre-1.0: schema is changing fast, migration
  authoring keeps up at high cost. Better to defer to post-1.0 once
  the schema stabilizes.
- **C. Refuse + clear error + --reset.** Adopted. The new code refuses
  to load older / unrecognized snapshots and points the operator at
  `reyn chat --reset` as the documented remediation.

## Decision

**Adopt C: refuse + --reset; defer migration to post-1.0 (R-D15).**

Implementation:

- `SNAPSHOT_VERSION` and `SKILL_SNAPSHOT_VERSION` constants in
  `agent_snapshot.py` and `skill_snapshot.py`.
- `SchemaVersionError` exception, raised by `load()` when the file's
  `version` field doesn't match.
- Error message format:

  ```
  AgentSnapshot at /path/to/snapshot.json has version 1, expected 2.
  Run `reyn chat --reset` to wipe in-flight skill state (audit logs
  in .reyn/events/ are preserved).
  ```

- `reyn chat` CLI catches `SchemaVersionError` from `restore_all` and
  exits cleanly with the message (no stack trace).
- `reyn chat --reset` (ADR-0010) is the documented remediation:
  wipes snapshots + WAL but preserves `events/` (P6 audit truth).

Trade-off explicitly chosen: **system integrity > user data
preservation** during pre-1.0 schema bumps. Lost in-flight skill state
is preferable to silent corruption.

Release-time mechanic: when a breaking change ships, bump
`SNAPSHOT_VERSION` and document `--reset` in release notes.

## Consequences

**Positive:**

- Operators see actionable errors, not stack traces.
- Code stays simple (no migration authoring during fast iteration).
- Audit logs (`events/`) survive resets — historical analysis is
  unaffected.
- `--reset` path is also useful for "broken state, can't figure out
  why" debugging.

**Negative:**

- User data loss during upgrade: in-flight skills are gone after
  `--reset`. Acceptable in pre-1.0; users are warned.
- Encourages ad-hoc schema changes without migration; could backfire
  if schema thrashes too much. Mitigated by reviewing schema bumps
  carefully.

**Precluded:**

- Cannot upgrade-in-place during pre-1.0. Documented as the trade-off.
- Migration framework (R-D15) is the post-1.0 commitment that
  replaces this policy.

## References

- Commit `7ba4303` — U4 schema_version refuse machinery
- Commit `0a8c654` — U5 chat CLI graceful exit on SchemaVersionError
- [docs/en/reference/upgrade-policy.md](../reference/upgrade-policy.md) —
  operator-facing version of this policy
- ADR-0010 (CLI flags — defines `--reset`)
- R-D15 (post-1.0 migration framework)
