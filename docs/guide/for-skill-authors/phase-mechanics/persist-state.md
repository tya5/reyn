---
type: how-to
topic: config
audience: [human]
applies_to: [.reyn/, reyn.yaml]
---

# Manage persisted state

**Goal:** Decide what reyn state to commit, what to gitignore, and where to put it.

## What lives under `.reyn/`

`.reyn/` is an **opaque runtime-state directory** — treat it as tool-managed, not human-edited.
The full per-subtree inventory + classification (recovery-core `state/`+`config/`, persist
`memory/`+`approvals.yaml`, audit `events/`, derived `cache/`, operator-owned outside) is the
canonical **[The `.reyn/` directory layout](../../../reference/runtime/reyn-dir-layout.md)** —
including the **recovery-core write-gate** (mutate config via the dedicated ops, never a raw
`file.write` to `.reyn/config/` or `.reyn/state/`).

`reyn.yaml` (the project config) is checked in. Personal overrides go in
`reyn.local.yaml` (gitignored, project root) — not in `.reyn/config.yaml`.

**Note:** `.reyn/config.yaml` has been deprecated. If it exists, Reyn warns
and ignores it. Move its contents to `reyn.local.yaml`.

## Recommended `.gitignore`

```
.reyn/
reyn.local.yaml
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

- `~/.reyn/config.yaml` — user-global defaults (your default model, API base, etc.). This file is still active (not deprecated).
- `~/.reyn/memory/` — global memory (facts about you across all projects).

`recall_memory` reads both global and project scopes.

## What's safe to delete

| Path | Safe to delete? | Notes |
|------|-----------------|-------|
| `.reyn/events/` | Yes | Just logs. You lose replay data. |
| `.reyn/eval-results/` | Yes | Regenerable. |
| `.reyn/agents/` | Yes | You lose agent profiles, chat history, and skill-resume checkpoints. |
| `.reyn/approvals.yaml` | Yes | You'll be re-prompted on the next run. |
| `.reyn/memory/` | Maybe | You lose persisted facts. Export first: `reyn memory export --out memory.json`. |

`reyn.yaml` and `reyn.local.yaml` are config; deleting them resets to defaults.

## See also

- [Reference: state-dir](../../../reference/config/state-dir.md)
- [Reference: reyn.yaml](../../../reference/config/reyn-yaml.md) — `state_dir` key
- [Concepts: memory](../../../concepts/data-retrieval/memory.md)
