# Monitor and improve with cron

A common pattern: wake an agent periodically, have it read what happened
since the last wake, and let it act on that — flag a stuck task, tighten a
prompt, adjust a pipeline. This guide builds that pattern out of two
existing pieces, `cron.jobs` and `.reyn/events/`, with no new mechanism.

## Why pull, not push

reyn also has a **push** mechanism for reaching another session directly —
a hook's `template_push` / `shell_push` fields can target a specific
`session` (see the [`hooks` block](../../reference/config/reyn-yaml.md) field
table). That is the right tool when a session needs to actively notify
another one at the moment something happens.

For *monitoring*, pull is the better fit and the recommended pattern:

- The monitored session does nothing to cooperate — no hook to register, no
  awareness that it's being watched. Everything it needs already lands in
  its own audit log.
- The monitor reads on its own schedule, from a log that already exists.
  There's nothing to wire on the monitored side, and nothing to break there
  either.

## 1. Wake a monitoring agent on a schedule

Add a `cron` job that dispatches a message to the agent that will do the
monitoring:

```yaml
cron:
  jobs:
    - name: watch_and_improve
      to: improver_agent
      message: "Check .reyn/events/ for anything since your last run and act on it."
      schedule: "*/30 * * * *"   # every 30 minutes
      enabled: true
```

See the [`cron` block reference](../../reference/config/reyn-yaml.md) for the
full field list, and [`reyn cron run/list/status`](../../reference/cli/cron.md)
for running the scheduler standalone.

## 2. Read what happened from the event log

Every project writes a structured, append-only event log under
`.reyn/events/` — one JSONL file per run, nested under
`agents/<agent>/.../<YYYY-MM>/`. `reyn events` replays it:

```bash
# a single run's log
reyn events .reyn/events/agents/improver_agent/chat/2026-08/abc123.jsonl

# every log under a directory, walked recursively, oldest first
reyn events .reyn/events/agents/ --since 2026-08-01

# only completion-shaped events
reyn events .reyn/events/agents/ --filter turn_settled --filter session_completed
```

For "what finished since I last looked," the completion-shaped kinds are
the ones worth filtering for (see the full
[kind vocabulary](../../reference/runtime/events.md) for the closed set):

| Kind | Signals |
|---|---|
| `turn_settled` | a turn reached a terminal state; payload carries `kind` and, when the turn was part of a chain, `chain_id` |
| `session_completed` | a session ended; payload carries `agent_name` |
| `pipeline_step_completed` | one pipeline step finished; payload carries `run_id`, `step_index`, `step_kind`, `total_steps` |
| `chain_timeout` | a chain gave up waiting on a reply; payload carries `chain_id`, `waiting_on`, `timeout_seconds`, `origin_agent` |
| `task_settle_undelivered` | a completion couldn't be delivered back to its waiter; payload carries `run_id`, `reply_to_agent`, `reply_to_sid`, `reason` |

`--since`/`--until` bound the walk to files in that date range;
`reyn events purge --before <DATE>` trims old logs once the monitor no
longer needs them.

## 3. Act on it

The improving agent's own message (step 1) can name the action directly —
"flag anything that timed out," "if the same pipeline step keeps failing,
propose a fix" — and it has the ordinary agent surface (editing a prompt
file, a pipeline YAML, filing a note) to act with. Nothing about steps 1–2
requires the monitored session to do anything differently, or requires the
monitoring agent to run inside the same session at all.

## See also

- [`cron` block reference](../../reference/config/reyn-yaml.md)
- [`reyn cron` CLI reference](../../reference/cli/cron.md)
- [Events reference](../../reference/runtime/events.md) — full kind vocabulary and payload documentation
- [`reyn events` CLI reference](../../reference/cli/events.md)
