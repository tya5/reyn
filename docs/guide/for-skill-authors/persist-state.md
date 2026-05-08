---
type: how-to
topic: config
audience: [human]
applies_to: [.reyn/, reyn.yaml]
---

# Manage persisted state

**Goal:** Decide what reyn state to commit, what to gitignore, and where to put it.

## What lives under `.reyn/`

| Path | Purpose | Default git status |
|------|---------|--------------------|
| `.reyn/config.yaml` | Personal overrides for `reyn.yaml` | gitignore |
| `.reyn/approvals.yaml` | Saved permission approvals | gitignore |
| `.reyn/events/` | Per-run event JSONL logs | gitignore |
| `.reyn/chats/` | Chat session histories | gitignore |
| `.reyn/eval_reports/` | Eval results per skill | gitignore |
| `.reyn/memory/` | Project-scoped memory | depends on the team |

`reyn.yaml` (the project config) is checked in. `.reyn/config.yaml` (personal) is not.

## Recommended `.gitignore`

```
.reyn/config.yaml
.reyn/approvals.yaml
.reyn/events/
.reyn/chats/
.reyn/eval_reports/
```

Memory is a judgment call:

- **Commit `.reyn/memory/`** when the project memory is shared knowledge (conventions, decisions) and collaborators benefit from it.
- **Gitignore it** when memory is per-developer notes you don't want to push.

## Move state elsewhere

The default location is `<project_root>/.reyn/`. Override per-project:

```yaml
# reyn.yaml
state_dir: /var/lib/reyn/<project>
```

Or per-run via `--state-dir` (when supported by the subcommand) — generally the project setting is enough.

## Global state

`~/.reyn/` mirrors the per-project shape:

- `~/.reyn/config.yaml` — user-global defaults (your default model, API base, etc.).
- `~/.reyn/memory/` — global memory (facts about you across all projects).

`recall_memory` reads both global and project scopes.

## What's safe to delete

| Path | Safe to delete? | Notes |
|------|-----------------|-------|
| `.reyn/events/` | Yes | Just logs. You lose replay data. |
| `.reyn/eval_reports/` | Yes | Regenerable. |
| `.reyn/chats/` | Yes | You lose the ability to resume sessions. |
| `.reyn/approvals.yaml` | Yes | You'll be re-prompted on the next run. |
| `.reyn/memory/` | Maybe | You lose persisted facts. Export first: `reyn memory export --out memory.json`. |

`reyn.yaml` and `.reyn/config.yaml` are config; deleting them resets to defaults.

## See also

- [Reference: state-dir](../../reference/config/state-dir.md)
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `state_dir` key
- [Concepts: memory](../../concepts/memory.md)
