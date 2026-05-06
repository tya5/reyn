# weekly-summary

> 🔮 **Roadmap example.** Depends on: native scheduling (`reyn schedule`),
> long-running / daemon mode, and a built-in scoped state store. Not
> runnable on Reyn v1 as of 2026-05-02.
>
> Tracked in: post-OSS roadmap (long-running jobs item in the residuals
> snapshot). A workaround using external `cron` + `reyn run` is documented
> below.

A pattern stub for "every Monday at 09:00, summarize what changed in repo
X over the past week and post the result." Reyn v1 has no built-in cron
or daemon mode; this recipe documents the **shape** of the workflow so
you can wire it up with an external scheduler today, and so it's clear
what we'd need natively to make this first-class.

## What this would show (when supported)

- Long-running / scheduled triggers as a first-class concept.
- Persisting state across runs (last-seen commit hash, prior summaries).
- Idempotent re-runs (same week → same artifact, no duplicates).

## Workaround for v1: external cron + `reyn run`

Today you can get most of the way with `cron` (or any scheduler) calling
`reyn run`:

```cron
# crontab -e
0 9 * * MON  cd ~/myrepo && reyn run weekly_summary "summarize last week" >> .reyn/weekly.log 2>&1
```

The `weekly_summary` skill (you'd write it under `reyn/local/`) would:

1. Read `.reyn/state/weekly_summary.json` for `last_run_iso`.
2. Run `git log --since=$last_run_iso --until=now`.
3. Synthesize the summary.
4. Write the new `last_run_iso` back to state.

Steps 1 and 4 need `read_file` / `write_file` Control IR ops with
permission grants in `reyn.yaml`.

## Sketch — `weekly_summary` skill graph

```
load_state → collect_changes → summarize → save_state
```

A schema-stub for the input artifact and a graph stub are not included
yet because the persistent-state story (PR-???) is still open — naming
and on-disk layout will affect the artifact shape.

## What's missing for native support

- A `reyn schedule` CLI surface (`reyn schedule add weekly_summary --cron "0 9 * * MON"`).
- A daemon / supervisor that owns scheduled invocations and surfaces
  failures.
- Built-in scoped state storage (today possible via files, but ad hoc).
- An idempotency key concept on runs.

When those land, this recipe becomes a real one — replace this README
with the v1.x version.

## See also

- [Concepts: workspace](../../docs/en/concepts/workspace.md) — where
  cross-run state lives today.
- [How-to: persist state](../../docs/en/how-to/persist-state.md)
