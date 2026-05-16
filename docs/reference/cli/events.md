---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn events]
---

# `reyn events`

Replay or purge P6 event log files in `.reyn/events/`.

## Synopsis

```
reyn events <PATH> [OPTIONS]
reyn events purge [OPTIONS]
```

## Modes

| Mode | Description |
|------|-------------|
| *(positional PATH)* | Stream events from a JSONL file to stdout. |
| `purge` | Delete event files older than a given date. |

## Options — replay mode

| Flag | Default | Description |
|------|---------|-------------|
| `--filter TYPE` | all | Include only events of this kind. Repeatable. |
| `--skip TYPE` | none | Exclude events of this kind. Repeatable. |
| `--conversation` | off | Group output by run boundary. |
| `--since YYYY-MM-DD` | beginning | Skip files and events before this date (inclusive). |
| `--until YYYY-MM-DD` | now | Skip files and events after this date (inclusive). |

## Options — purge mode

| Flag | Default | Description |
|------|---------|-------------|
| `--before DATE` | required | Delete files before this ISO date (YYYY-MM-DD). |
| `--agent NAME` | all | Limit purge to one agent's event directory. |
| `--dry-run` | off | Print files that would be deleted without removing them. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Path not found or I/O error. |

## Examples

```bash
reyn events .reyn/events/direct/skill_runs/2026-05/abc123_my_skill.jsonl
reyn events .reyn/events/direct/skill_runs/2026-05/abc123.jsonl --filter phase_started
reyn events purge --before 2026-04-01
reyn events purge --before 2026-04-01 --dry-run
```
