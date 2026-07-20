---
type: reference
topic: config
audience: [human, agent]
applies_to: [.reyn/]
---

# `.reyn/` — state directory

Per-project state. Location: `<project_root>/.reyn/` — fixed. (There is no `state_dir` knob in `reyn.yaml`; the runtime constructs the path from the discovered project root.)

## Layout

The canonical `.reyn/` directory layout — every subtree, the recovery-core /
persist / audit / cache / outside classification, and the recovery-core write-gate — is
documented in **[The `.reyn/` directory layout](../runtime/reyn-dir-layout.md)**. See there
for the full tree and "where a new subsystem puts its data". This page covers `--state-dir`
routing specifically.

**Note:** `.reyn/config.yaml` has been removed.
Personal config overrides now live in `reyn.local.yaml` (gitignored, project root).
If you have an existing `.reyn/config.yaml`, move its contents to `reyn.local.yaml`
and delete the old file. Reyn will print a warning until it is removed.

### `approvals.yaml`

Persistent permission approvals from interactive prompts. Keyed by `<skill>/<op>/<path>` — see [permissions.md](permissions.md).

```yaml
my_skill/file.write//tmp/output: just_path
my_skill/shell: allow
```

Inspect with `reyn permissions list`. Remove with `reyn permissions revoke <key>`.

### `events/`

All event JSONL logs. Organized by caller and log type:

- `direct/skill_runs/<YYYY-MM>/<ts>_<skill>.jsonl` — events from `reyn run` (non-agent workflow runs)
- `agents/<name>/skill_runs/<YYYY-MM>/<ts>_<skill>.jsonl` — workflow run events spawned by a named agent
- `agents/<name>/chat/<YYYY-MM>/<ts>.jsonl` — chat-session events (rotated by `events.max_bytes` / `events.max_age_seconds`)

JSONL files are replayable with `reyn events <file>`. See [events reference](../runtime/events.md).

### `agents/<name>/`

Per-agent workspace. One directory per named agent (created by `reyn agent new`). The `default` agent always exists.

- `profile.yaml` — agent identity: name, role, optional `allowed_mcp`. See profile-yaml reference.
- `history.jsonl` — append-only conversation log (user + assistant turns; cross-agent messages include `chain_id` for tracing).
- `memory/` — agent-scoped memory (`MEMORY.md` index + body files). Recalled and written automatically during the router phase.
- `state/skills/<run_id>.snapshot.json` — WAL snapshots for crash recovery of in-flight skill runs.

### `skill-versions/<name>/`

Skill version snapshots written by `skill_improver`. Each `v<N>.md` is a timestamped snapshot of `skill.md` at the time a proposal was applied. Pruned to `self_improvement.max_versions` snapshots. Inspect with `reyn skill versions <name>`.

### `state/budget_ledger.jsonl`

Durable, append-only budget record log (fsync per append). Holds one record per LLM call (token + USD usage). Legacy per-chain skill-spawn records (`kind: "spawn"`) may still be present in an old ledger but are no longer written and are skipped on read. On startup Reyn re-aggregates the daily / monthly totals (auto-reset at midnight / the 1st of the month) and the cumulative per-agent token + USD totals — so every budget cap survives a process restart or crash. This is the cap-critical source of truth. Inspect with `/budget` in `reyn chat`. Not affected by `/budget reset` (which only clears in-memory counters).

Because the ledger is never rotated, `hydrate` does not re-parse it in full on
every startup (#2945) — it reads a compacted per-agent checkpoint (see
`state/../cache/budget_checkpoint.json` below) and only re-parses the tail
written since that checkpoint's anchor. If the checkpoint is missing or
corrupt, `hydrate` falls back to a full re-scan. If the ledger was truncated
below the checkpoint's anchor (including deleted entirely), the checkpoint's
per-agent totals are merged in as a **floor** on top of that re-scan — never
silently discarded — so a truncated/lost ledger can never under-count a
cap-critical per-agent total. A ledger that is instead *replaced* with
different content of the same size or larger (content mismatch without
shrinking) is NOT floored — only its full re-scan is trusted.

### `state/budget_state.json`

A throttled, best-effort snapshot of the in-memory budget counters, written on a short interval as a convenience cache on top of the ledger. It can lag the ledger by up to a second, so on recovery the ledger value always wins. Safe to delete; the ledger is the authoritative store.

### `cache/budget_checkpoint.json`

A compacted, point-in-time summary of `budget_ledger.jsonl`'s per-agent
lifetime totals, anchored to an exact byte position in the ledger (#2945).
Refreshed automatically alongside `budget_state.json`. A write failure here
(read-only directory, disk full) is logged and swallowed — it never blocks
startup, since this file is DERIVED/cache and can always be rebuilt from the
ledger.

Safe to delete for correctness (`hydrate` reconstructs from the ledger, at
the cost of a full re-scan) but **not** equivalent to "reset the per-agent
cap": deleting/archiving *only* the ledger while this checkpoint still
exists does NOT reset the per-agent totals — they survive as a floor (see
`state/budget_ledger.jsonl` above). To actually reset per-agent spend,
archive both files together while the process is stopped.

### `memory/`

Project-scope memory — facts that should persist across runs but are project-specific. Global memory lives at `~/.reyn/memory/` instead.

`MEMORY.md` is the index; each `<name>.md` is one memory entry with frontmatter (`type`, `name`, `description`).

## Global state (`~/.reyn/`)

Same shape as `.reyn/` but lives in the home directory. Used for:

- `~/.reyn/config.yaml` — user-global defaults.
- `~/.reyn/memory/` — global memory (facts about the user, not tied to a project).

`recall_memory` and `write_memory` consult both global and project scopes.

## Gitignore

Recommended additions:

```
.reyn/
reyn.local.yaml
```

Memory (`.reyn/memory/`) — choose based on whether project memory is shared between collaborators.

## See also

- [reyn-yaml.md](reyn-yaml.md) — `state_dir` setting
- [permissions.md](permissions.md) — approvals.yaml details
- [Reference: events](../runtime/events.md)
