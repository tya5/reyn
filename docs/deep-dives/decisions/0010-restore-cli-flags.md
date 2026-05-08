# ADR-0010: --no-restore / --reset CLI flag semantics

**Status**: Accepted (2026-05-03)
**Track**: PR-resume-ux β U3 + U4

## Context

Default `reyn chat` automatically calls `AgentRegistry.restore_all()`
to load in-flight skill state from snapshots and replay the WAL.
This is the right default for users — they expect their interrupted
work to come back.

But there are scenarios where automatic restore is wrong:

- **Debug**: developer wants to start a clean session without losing
  the persisted state (= still want it to come back next run).
- **Schema mismatch**: `SchemaVersionError` (ADR-0006) tells the user
  to wipe state. They need a tool to actually wipe.
- **Broken state**: snapshot or WAL corruption causes weird behavior.
  Operator needs an escape hatch.

Two distinct operations are needed: "skip restore this time" vs "wipe
state".

## Considered alternatives

- **A. Single `--reset` flag covering both.** Rejected: conflates
  "skip" (non-destructive) with "wipe" (destructive). Users would
  use `--reset` casually and lose data.
- **B. `--no-restore` only; rely on manual `rm` for wipe.** Rejected:
  manual `rm` is error-prone (which files? what about the events
  dir?), and `SchemaVersionError`'s hint message wouldn't have a
  clean command to point at.
- **C. Two flags with distinct semantics.** Adopted. Mutually
  meaningful — `--reset` then start with empty state; `--no-restore`
  then start without loading (state stays for next run).

## Decision

**Adopt C: two distinct flags.**

| Flag | snapshot/WAL on disk | next run |
|---|---|---|
| (none, default) | restore_all + bulk-prompt at startup | normal restore |
| `--no-restore` | preserved + warning banner | normal restore |
| `--reset` | **deleted** (with confirm) | empty state |

`--reset` semantics:

- Wipes:
  - `.reyn/state/wal.jsonl`
  - `.reyn/agents/<name>/state/snapshot.json`
  - `.reyn/agents/<name>/state/skills/`
- Preserves:
  - `.reyn/events/` (P6 audit truth — never wiped by `--reset`)
- Confirmation: input prompt before deletion. `yes` proceeds; anything
  else aborts.
- Idempotent on already-clean state.

`--no-restore` semantics:

- Skip the `restore_all()` call this run.
- Print banner to stderr: "⚠ skill state on disk is NOT loaded".
- State files unchanged on disk.
- Next run (without the flag) loads them normally.

Implementation lives in `_reset_project_state(project_root, *,
confirm=True)` for testability + the run() function's flag handling.

## Consequences

**Positive:**

- Clear separation: non-destructive (`--no-restore`) vs destructive
  (`--reset`).
- `--reset` is the documented remediation for schema mismatches
  (ADR-0006) and other broken-state scenarios.
- Audit logs (`events/`) are never touched — operators can analyze
  forensics across resets.
- Helper function is unit-testable (no event loop required).

**Negative:**

- Two flags = two things for users to learn. Mitigated by the
  `--help` output's separate descriptions.
- `--reset` confirmation is interactive (`input()`). Scripts can't
  pipe `yes |` cleanly because the prompt is on the same line.
  Acceptable: scripted resets in CI / automation should set up clean
  state via other means (e.g. tmpdir).

**Precluded:**

- `--reset --force` (skip confirmation) for scripting. Could be added
  later if there's demand. Currently the friction is intentional —
  reset is destructive and shouldn't be one keystroke.

## References

- Commit `3b90902` — U3 CLI flag implementation
- ADR-0006 (schema version policy — `--reset` is its remediation)
- [docs/en/reference/upgrade-policy.md](../reference/upgrade-policy.md)
