---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn permissions]
---

# `reyn permissions`

Inspect and manage saved permission approvals in `.reyn/approvals.yaml`.

## Synopsis

```
reyn permissions list
reyn permissions revoke <key>
reyn permissions clear [--yes]
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `list` | Print all saved approvals with their keys. |
| `revoke <key>` | Remove a single approval entry by key. |
| `clear` | Remove all saved approvals. Prompts unless `--yes`. |

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--yes`, `-y` | off | Skip confirmation on `clear`. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Key not found, or I/O error. |

## Examples

```bash
reyn permissions list
reyn permissions revoke "file.read//home/user/project"
reyn permissions clear --yes
```

## See also

- [Reference: permissions](../config/permissions.md)
