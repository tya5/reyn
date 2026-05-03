# Upgrade policy

How Reyn handles schema changes between releases.

## Pre-1.0 (current)

Snapshot schemas (per-agent + per-skill) and the WAL format may change
incompatibly between releases. When that happens, the new release will
refuse to load the older snapshot rather than silently corrupt state.

If you upgrade Reyn and see this on startup:

```
AgentSnapshot at /path/to/.reyn/agents/<name>/state/snapshot.json has
version 1, expected 2. Run `reyn chat --reset` to wipe in-flight skill
state (audit logs in .reyn/events/ are preserved).
```

…run the suggested command:

```bash
reyn chat --reset
```

This wipes:

- `.reyn/state/wal.jsonl` (process WAL)
- `.reyn/agents/<name>/state/snapshot.json` (per-agent snapshots)
- `.reyn/agents/<name>/state/skills/` (per-skill snapshots)

Audit logs under `.reyn/events/` are **preserved** — they are the P6
audit truth and never wiped by `--reset`.

The trade-off is explicit: in-flight skill state (the work you'd resume
after a crash) is lost during a schema-changing upgrade. This is
preferable to running new code against stale snapshot fields, which
could produce subtle bugs that are hard to diagnose.

## Post-1.0 (planned)

Once Reyn commits to schema stability for 1.0, automated migration will
replace the refuse-and-reset model. Schema bumps will trigger a
migration chain that translates old snapshot formats to new in place,
preserving in-flight state across upgrades.

Tracked as R-D15 in the project plan. Migration framework will live
under `src/reyn/skill/migrations/` and run during snapshot load.

## Related flags

```bash
reyn chat --no-restore  # skip restore this run; state stays for next run
reyn chat --reset       # wipe state with confirmation prompt
```

`--no-restore` is for debug / temporary fresh-start without losing data.
`--reset` is destructive (with confirmation) and is the documented
remediation for schema mismatches.
