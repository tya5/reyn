---
type: reference
topic: config
audience: [human, agent]
applies_to: [.reyn/]
---

# `.reyn/` — state directory

Per-project state. Location: `<project_root>/.reyn/` — fixed. (There is no `state_dir` knob in `reyn.yaml`; the runtime constructs the path from the discovered project root.)

## Layout

```
.reyn/
├── approvals.yaml                          # persistent permission approvals
├── events/                                 # all event JSONL logs
│   ├── direct/                             # skill runs from `reyn run`
│   │   └── skill_runs/<YYYY-MM>/
│   │       └── <ts>_<skill>.jsonl
│   └── agents/<name>/                      # skill runs + chat events from an agent
│       ├── skill_runs/<YYYY-MM>/
│       │   └── <ts>_<skill>.jsonl
│       └── chat/<YYYY-MM>/                 # chat session events (rotated by size/age)
│           └── <ts>.jsonl
├── agents/<name>/                          # per-agent workspace (one dir per agent)
│   ├── profile.yaml                        # agent name, role, allowed_skills
│   ├── history.jsonl                       # append-only conversation log
│   ├── memory/                             # agent-scoped memory
│   │   ├── MEMORY.md
│   │   └── <name>.md
│   └── state/                              # WAL skill-run snapshots
│       └── skills/<run_id>.snapshot.json
├── skill-versions/<name>/                  # skill version snapshots
│   └── v<N>.md
├── eval-results/<skill>/                   # `reyn eval run` result files
│   └── <timestamp>.jsonl
├── state/                                  # process-global persistent state
│   ├── budget_ledger.jsonl                 # durable budget ledger (daily/monthly/per-agent + spawn caps)
│   └── budget_state.json                   # throttled best-effort cache over the ledger
└── memory/                                 # project-scope memory
    ├── MEMORY.md
    └── <name>.md
```

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

- `direct/skill_runs/<YYYY-MM>/<ts>_<skill>.jsonl` — events from `reyn run` (non-agent skill runs)
- `agents/<name>/skill_runs/<YYYY-MM>/<ts>_<skill>.jsonl` — skill run events spawned by a named agent
- `agents/<name>/chat/<YYYY-MM>/<ts>.jsonl` — chat-session events (rotated by `events.max_bytes` / `events.max_age_seconds`)

JSONL files are replayable with `reyn events <file>`. See [events reference](../runtime/events.md).

### `agents/<name>/`

Per-agent workspace. One directory per named agent (created by `reyn agent new`). The `default` agent always exists.

- `profile.yaml` — agent identity: name, role, optional `allowed_skills`. See [profile-yaml reference](../dsl/profile-yaml.md).
- `history.jsonl` — append-only conversation log (user + assistant turns; cross-agent messages include `chain_id` for tracing).
- `memory/` — agent-scoped memory (`MEMORY.md` index + body files). Recalled and written automatically during the router phase.
- `state/skills/<run_id>.snapshot.json` — WAL snapshots for crash recovery of in-flight skill runs.

### `skill-versions/<name>/`

Skill version snapshots written by `skill_improver`. Each `v<N>.md` is a timestamped snapshot of `skill.md` at the time a proposal was applied. Pruned to `self_improvement.max_versions` snapshots. Inspect with `reyn skill versions <name>`.

### `eval-results/<skill>/`

One JSONL file per `reyn eval run` execution. Each line records a single case result: input, expected, actual `final_output`, score, passed flag, and `skill_version_hash`. Used by `reyn eval report` and `reyn eval compare`.

### `state/budget_ledger.jsonl`

Durable, append-only budget record log (fsync per append). Holds one record per LLM call (token + USD usage) and one record per skill spawn (`kind: "spawn"`). On startup Reyn re-aggregates the daily / monthly totals (auto-reset at midnight / the 1st of the month), the cumulative per-agent token + USD totals, and the per-chain spawn counts — so every budget cap survives a process restart or crash. This is the cap-critical source of truth. Inspect with `/budget` in `reyn chat`. Not affected by `/budget reset` (which only clears in-memory counters).

### `state/budget_state.json`

A throttled, best-effort snapshot of the in-memory budget counters, written on a short interval as a convenience cache on top of the ledger. It can lag the ledger by up to a second, so on recovery the ledger value always wins. Safe to delete; the ledger is the authoritative store.

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
