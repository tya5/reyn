---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn init]
---

# `reyn init`

Scaffold a new Reyn project in the current directory.

## Synopsis

```
reyn init
```

## What it creates

| Path | Action |
|------|--------|
| `reyn.yaml` | Minimal project config. Skipped if already exists. |
| `reyn.local.yaml.example` | Annotated personal override example. Skipped if already exists. |
| `.reyn/` | Runtime state directory. Created if missing. |
| `.gitignore` | Appended with `.reyn/` and `reyn.local.yaml` if not already present. |

## Options

None. The command is idempotent — safe to re-run.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Always (idempotent). |

## Examples

```bash
mkdir my-project && cd my-project
reyn init
```

## See also

- [Reference: `reyn.yaml`](../config/reyn-yaml.md)
