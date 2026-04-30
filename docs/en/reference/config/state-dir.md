---
type: reference
topic: config
audience: [human, agent]
applies_to: [.reyn/]
---

# `.reyn/` — state directory

Per-project state. Default location: `<project_root>/.reyn/`. Override via `reyn.yaml`'s `state_dir` key.

## Layout

```
.reyn/
├── config.yaml          # personal overrides (often gitignored)
├── approvals.yaml       # persistent permission approvals
├── events/              # event JSONL logs, one file per run
│   └── <run_id>.jsonl
├── chats/               # chat session state (one file per session)
│   └── <session_id>.json
└── memory/              # project-scope memory
    ├── MEMORY.md
    └── <name>.md
```

### `config.yaml`

Personal overrides for `reyn.yaml`. Same schema. Typically gitignored. Use for `api_base`, custom `models`, etc.

### `approvals.yaml`

Persistent permission approvals from interactive prompts. Keyed by `<skill>/<op>/<path>` — see [permissions.md](permissions.md).

```yaml
my_skill/file.write//tmp/output: just_path
my_skill/shell: allow
```

Inspect with `reyn permissions list`. Remove with `reyn permissions revoke <key>`.

### `events/<run_id>.jsonl`

JSONL log of all events emitted during a run. Replayable with `reyn events <file>`. See [events reference](../runtime/events.md).

### `chats/<session_id>.json`

State for a `reyn chat` session: history, persisted memory recall results, etc.

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
.reyn/config.yaml
.reyn/events/
.reyn/chats/
.reyn/approvals.yaml
```

Memory (`.reyn/memory/`) — choose based on whether project memory is shared between collaborators.

## See also

- [reyn-yaml.md](reyn-yaml.md) — `state_dir` setting
- [permissions.md](permissions.md) — approvals.yaml details
- [Reference: events](../runtime/events.md)
