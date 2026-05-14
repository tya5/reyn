---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn memory]
---

# `reyn memory`

Manage stored agent memories in `.reyn/agents/<name>/memory/` or the shared layer.

## Synopsis

```
reyn memory list    [--agent NAME]
reyn memory show    <name> [--agent NAME]
reyn memory edit    <name> [--agent NAME]
reyn memory delete  <name> [--agent NAME] [--yes]
reyn memory search  <pattern> [--agent NAME] [--ignore-case]
reyn memory export  [--agent NAME] [--out PATH]
reyn memory import  <file> [--agent NAME] [--overwrite]
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `list` | List all memory files for the agent or shared layer. |
| `show <name>` | Print the contents of a single memory file. |
| `edit <name>` | Open the memory file in `$EDITOR`. |
| `delete <name>` | Delete a memory file. Prompts for confirmation unless `--yes`. |
| `search <pattern>` | Grep memory files for a regex pattern. |
| `export` | Dump all memories as JSON to stdout or `--out PATH`. |
| `import <file>` | Import memories from a JSON file. Skips existing unless `--overwrite`. |

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--agent NAME` | shared layer | Target a named agent's memory directory. |
| `--yes`, `-y` | off | Skip confirmation on `delete`. |
| `--ignore-case`, `-i` | off | Case-insensitive match for `search`. |
| `--out PATH` | stdout | Output path for `export`. |
| `--overwrite` | off | Overwrite existing files on `import`. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Memory or agent not found, or I/O error. |

## Examples

```bash
reyn memory list
reyn memory list --agent my_agent
reyn memory show preferences
reyn memory search "API key" --ignore-case
reyn memory export --out backup.json
reyn memory import backup.json --overwrite
```
