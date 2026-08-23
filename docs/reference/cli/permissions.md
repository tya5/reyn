---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn permissions]
---

# `reyn permissions`

Inspect and manage saved permission approvals in `.reyn/approvals.jsonl` (an append-only ledger — see [Reference: permissions](../config/permissions.md) for the mechanism).

## Synopsis

```
reyn permissions list
reyn permissions revoke <key>
reyn permissions clear [--yes]
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `list` | Print all currently-approved keys (the ledger folded). |
| `revoke <key>` | Revoke a single key — appends an `approved=False` record; the earlier grant stays in the ledger's history, never deleted. |
| `clear` | Revoke every currently-approved key the same way. Prompts unless `--yes`. |

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
