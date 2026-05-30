---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn cron]
---

# `reyn cron`

Run and inspect cron-scheduled skill jobs. Jobs are declared under `cron.jobs` in `reyn.yaml`; the scheduler dispatches each enabled job at its cron expression using the same headless `Agent.run` path as `reyn run`.

## Synopsis

```
reyn cron run
reyn cron list
reyn cron status
```

## Description

`reyn cron` manages time-triggered skill execution. The operator declares jobs in `reyn.yaml`; `reyn cron run` starts a foreground scheduler that fires each enabled job at its configured interval. `reyn cron list` and `reyn cron status` let operators inspect the job table without starting a scheduler.

## Subcommands

### `run`

Start the cron scheduler in the foreground. The command blocks until Ctrl-C is pressed.

```
reyn cron run
```

**Behaviour:**

1. Reads `cron.jobs` from `reyn.yaml`.
2. For each enabled job, computes the next fire time from the cron expression.
3. Prints a startup banner listing all enabled jobs and their next-run times.
4. Runs each job in a separate asyncio task; tasks sleep until the next fire time and then dispatch the skill via `Agent.run`.
5. On Ctrl-C, waits up to 5 seconds for in-flight jobs to finish, then exits cleanly.

**Example:**

```bash
$ reyn cron run
Started cron scheduler with 2 enabled job(s):
  • index_events_hourly  (0 */6 * * *)  next: 2026-05-16T18:00:00+00:00
  • weekly_ops_report    (0 9 * * MON)  next: 2026-05-19T09:00:00+00:00
^C
Cron scheduler stopped.
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Scheduler stopped normally (Ctrl-C or no jobs configured). |
| `1` | Fatal error during startup (e.g. `reyn.yaml` parse failure). |

### `list`

Print all configured cron jobs and their next computed fire time. No scheduler is started.

```
reyn cron list
```

**Output format:**

```
NAME                     SKILL          SCHEDULE        ENABLED  NEXT RUN
index_events_hourly      index_events   0 */6 * * *     true     2026-05-16T18:00:00+00:00
weekly_ops_report        ops_report     0 9 * * MON     true     2026-05-19T09:00:00+00:00
```

If no jobs are configured:

```bash
$ reyn cron list
(no jobs configured)
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Always (even when the table is empty). |

### `status`

Like `reyn cron list` but also shows last-run fields: `LAST RUN AT`, `LAST STATUS`, and `LAST ERROR`.

```
reyn cron status
```

> **v1 limitation:** last-run state is in-memory only. When invoked as a standalone command (i.e. not within a running `reyn cron run` session), all `last_run_*` fields are displayed as `-`. A future web-mode API (`/a2a/agents/cron/status`) will allow querying the live scheduler's state.

**Output format (standalone mode):**

```
NAME                     SKILL          SCHEDULE        ENABLED  NEXT RUN                    LAST RUN AT   LAST STATUS   LAST ERROR
index_events_hourly      index_events   0 */6 * * *     true     2026-05-16T18:00:00+00:00   -             -             -
weekly_ops_report        ops_report     0 9 * * MON     true     2026-05-19T09:00:00+00:00   -             -             -
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Always (even when the table is empty). |

## Configuration

Jobs are declared under `cron.jobs` in `reyn.yaml`. Each entry maps to one scheduled skill run.

```yaml
cron:
  jobs:
    - name: index_events_hourly
      skill: index_events
      schedule: "0 */6 * * *"
      input: {}
      enabled: true

    - name: weekly_ops_report
      skill: ops_report
      schedule: "0 9 * * MON"
      input:
        report_period: weekly
      enabled: true
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique job identifier. Used in log messages and (future) status queries. |
| `skill` | yes | Skill name to run. Resolved with the standard skill search order: `reyn/project/` → `reyn/local/` → stdlib. |
| `schedule` | yes | Five-field cron expression (minute hour day-of-month month day-of-week). |
| `input` | no | Initial input artifact passed to the skill as `Agent.run(skill, input)`. Defaults to `{}`. |
| `enabled` | no | Set `false` to disable without removing the entry. Defaults to `true`. |

See [Reference: `reyn.yaml`](../config/reyn-yaml.md) for the full schema.

## Cron expression syntax

Expressions follow the standard five-field format:

```
┌──────────── minute (0-59)
│ ┌────────── hour (0-23)
│ │ ┌──────── day of month (1-31)
│ │ │ ┌────── month (1-12 or JAN-DEC)
│ │ │ │ ┌──── day of week (0-7 or SUN-SAT; 0 and 7 = Sunday)
│ │ │ │ │
* * * * *
```

Common examples:

| Expression | Meaning |
|-----------|---------|
| `0 * * * *` | Every hour at minute 0 |
| `0 */6 * * *` | Every 6 hours |
| `0 9 * * MON` | Every Monday at 09:00 UTC |
| `30 4 1 * *` | First of each month at 04:30 UTC |

All times are UTC. The scheduler uses [`croniter`](https://pypi.org/project/croniter/) for expression parsing.

## Related

- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `cron:` configuration block
- [Concepts: Operational Intelligence](../../concepts/operational-intelligence.md) — use-cases for scheduled skill execution
- [Concepts: A2A protocol](../../concepts/a2a.md) — `RunRegistry` pattern and future web-mode status API
- [Reference: `reyn run`](run.md) — headless single-shot skill execution (same Agent.run path)
