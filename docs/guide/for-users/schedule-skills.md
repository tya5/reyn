---
type: how-to
topic: using-reyn
audience: [human]
---

# Run a skill on a schedule

Reyn can run a skill automatically on a cron schedule — for example, an
hourly event index or a weekly summary report. You declare jobs in
`reyn.yaml` under `cron.jobs`, then start the scheduler.

## Declare a job

Each job names a skill, a 5-field cron expression, and an optional input:

```yaml
# reyn.yaml
cron:
  jobs:
    - name: weekly_ops_report
      skill: ops_report
      schedule: "0 9 * * MON"   # every Monday at 09:00
      input:
        since_days: 7
      enabled: true
```

| Field | Required | Meaning |
|-------|----------|---------|
| `name` | yes | Unique job identifier |
| `skill` | yes | A stdlib or project skill to run |
| `schedule` | yes | 5-field cron expression (minute / hour / day-of-month / month / day-of-week) |
| `input` | no (default `{}`) | Input artifact passed to the skill |
| `enabled` | no (default `true`) | Set `false` to keep the entry but skip scheduling |

## Start the scheduler

```bash
reyn cron run
```

This runs in the **foreground** and blocks until you press Ctrl-C. It prints
a startup banner with each enabled job's next fire time, then dispatches each
job at its scheduled time using the same headless path as `reyn run`.

```
$ reyn cron run
Started cron scheduler with 1 enabled job(s):
  • weekly_ops_report  (0 9 * * MON)  next: 2026-05-19T09:00:00+00:00
^C
Cron scheduler stopped.
```

The scheduler also starts automatically inside `reyn web` (in the FastAPI
lifespan), so a running web server keeps your jobs firing without a separate
`reyn cron run` process.

## Inspect jobs without running

```bash
reyn cron list      # all jobs + next computed fire time
reyn cron status    # last-run state
```

`reyn cron list` reads `cron.jobs` and prints the table without starting a
scheduler.

## Notes

- **`reyn cron status` only reflects a live scheduler.** Last-run state is
  in-memory in v1, so outside an active `reyn cron run` session there is no
  persisted run history to show.
- Jobs run **headless** — there is no interactive prompt. Make sure any
  permissions the skill needs are pre-approved in `reyn.yaml` (a scheduled run
  cannot stop to ask you).
- Runtime-registered jobs live in `<project>/.reyn/cron.yaml` and override
  `reyn.yaml` `cron.jobs` on a name collision.

## See also

- [Reference: `reyn cron`](../../reference/cli/cron.md) — `run` / `list` / `status`, output formats, exit codes
- [Reference: `reyn.yaml` — `cron:` block](../../reference/config/reyn-yaml.md#cron-block) — full field schema
- [Cap your spending](cap-spending.md) — a scheduled job still counts against your daily / monthly budget
