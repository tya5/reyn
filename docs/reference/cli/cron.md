---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn cron]
---

# `reyn cron`

Run and inspect cron-scheduled jobs. Jobs are declared under `cron.jobs` in `reyn.yaml`; the scheduler dispatches each enabled job at its cron expression as a message (`to` + `message`) delivered to a named agent's inbox, tagged `sender="cron:<name>"`.

## Synopsis

```
reyn cron run
reyn cron list
reyn cron status
```

## Description

`reyn cron` manages time-triggered execution. The operator declares jobs in `reyn.yaml`; `reyn cron run` starts a foreground scheduler that fires each enabled job at its configured interval. `reyn cron list` and `reyn cron status` let operators inspect the job table without starting a scheduler.

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
4. Runs each job in a separate asyncio task; tasks sleep until the next fire time and then push the job's message into the target agent's inbox (`sender="cron:<name>"`), where the agent's router loop picks it up as a normal attributed turn. In standalone/foreground mode (no running `AgentRegistry`), dispatch reports an error instead of delivering — this mode is best suited to jobs whose target agent is otherwise attached to `reyn web`.
5. On Ctrl-C, waits up to 5 seconds for in-flight jobs to finish, then exits cleanly.

**Example:**

```bash
$ reyn cron run
Started cron scheduler with 2 enabled job(s):
  • morning_news       (0 9 * * *)     next: 2026-05-16T09:00:00+00:00
  • weekly_ops_report  (0 9 * * MON)   next: 2026-05-19T09:00:00+00:00
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
NAME                     TO             SCHEDULE        ENABLED  NEXT RUN
morning_news             news_agent     0 9 * * *       true     2026-05-16T09:00:00+00:00
weekly_ops_report        ops_agent      0 9 * * MON     true     2026-05-19T09:00:00+00:00
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
NAME                     TO             SCHEDULE        ENABLED  NEXT RUN                    LAST RUN AT   LAST STATUS   LAST ERROR
morning_news             news_agent     0 9 * * *       true     2026-05-16T09:00:00+00:00   -             -             -
weekly_ops_report        ops_agent      0 9 * * MON     true     2026-05-19T09:00:00+00:00   -             -             -
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Always (even when the table is empty). |

## Configuration

Jobs are declared under `cron.jobs` in `reyn.yaml`. Each entry maps to one scheduled run.

```yaml
cron:
  jobs:
    - name: morning_news
      to: news_agent
      message: "Summarize today's top news"
      schedule: "0 9 * * *"
      enabled: true

    - name: weekly_ops_report
      to: ops_agent
      message: "Generate the weekly ops report"
      schedule: "0 9 * * MON"
      enabled: true
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique job identifier. Used in log messages and status queries. |
| `to` | yes | Target agent name. The message is dispatched to its inbox with `sender="cron:<name>"`. |
| `message` | yes | Free-form text delivered to the target agent. |
| `schedule` | yes | Five-field cron expression (minute hour day-of-month month day-of-week). |
| `notify` | no | Opt-in unattended notification channel (e.g. `"telegram"`). Defaults to event-log only. |
| `input` | no | Extra input dict carried on the job. Defaults to `{}`. |
| `enabled` | no | Set `false` to disable without removing the entry. Defaults to `true`. |

A job shape with a bare `skill` name (no `to` + `message`) is rejected at config load with a `ValueError` — cron jobs are message-based, not direct skill invocations.

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
- [Concepts: Operational Intelligence](../../concepts/data-retrieval/operational-intelligence.md) — use-cases for scheduled execution
- [Concepts: A2A protocol](../../concepts/multi-agent/a2a.md) — `RunRegistry` pattern and future web-mode status API
- [Reference: `reyn run-once`](run-once.md) — headless single-shot agent invocation (a different dispatch path from cron's inbox-message delivery)
