---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn config]
---

# `reyn config`

Inspect and modify the effective Reyn configuration.

## Synopsis

```
reyn config [show]
reyn config fields
reyn config get <key>
reyn config set <key> <value>
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `show` | Print effective merged configuration as YAML (default). |
| `fields` | List all known keys with types and defaults. |
| `get <key>` | Print the value of a single dot-path key. |
| `set <key> <value>` | Write a key to `reyn.local.yaml`. Value is parsed as YAML. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Unknown key, or I/O error. |

## Notes

`reyn config set` always writes to `reyn.local.yaml` (gitignored) — never to `reyn.yaml`.

## Examples

```bash
reyn config
reyn config fields
reyn config get safety.loop.max_phase_visits
reyn config set model strong
reyn config set safety.loop.max_phase_visits 50
```

## See also

- [Reference: `reyn.yaml`](../config/reyn-yaml.md)
