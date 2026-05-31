"""`reyn cron` — cron-driven skill scheduling (FP-0009 Component B).

Subcommands:
  run     Start the foreground cron scheduler (blocks until Ctrl-C).
  list    Print all configured jobs with next-run time; no scheduler started.
  status  Like `list` but shows last-run fields too (empty in standalone mode).

The scheduler reads ``cron.jobs`` from reyn.yaml; each enabled job runs the
named skill on its cron schedule via the headless Agent.run path.

v1 limitation: last-run state is in-memory only.  ``reyn cron status``
shows empty last_run_* fields when invoked standalone (i.e. not while
``reyn cron run`` is active).  A future web-mode API will allow querying
the live scheduler.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def register(sub) -> None:
    p = sub.add_parser(
        "cron",
        help="Manage and run cron-scheduled skill jobs",
        description=(
            "Schedule and execute skills on a cron timetable.  "
            "Configure jobs under the ``cron.jobs`` key in reyn.yaml."
        ),
    )
    csub = p.add_subparsers(dest="cron_cmd", metavar="<subcommand>")
    csub.required = True
    p.set_defaults(func=_no_subcommand)

    # --- run ---
    csub.add_parser(
        "run",
        help="Start the foreground cron scheduler (blocks until Ctrl-C)",
        description=(
            "Start the cron scheduler in the foreground.  "
            "Each enabled job in reyn.yaml fires at its cron expression.  "
            "Press Ctrl-C to stop cleanly."
        ),
    ).set_defaults(func=run_run)

    # --- list ---
    csub.add_parser(
        "list",
        help="List configured cron jobs and their next-run time",
        description=(
            "Print all jobs from reyn.yaml cron.jobs with their schedule and "
            "next computed fire time.  No scheduler is started."
        ),
    ).set_defaults(func=run_list)

    # --- status ---
    status_p = csub.add_parser(
        "status",
        help="Show cron job status including last-run info (empty in standalone mode)",
        description=(
            "Print cron jobs with last-run fields (last_run_at, last_run_status, "
            "last_run_error).  In v1 these are always empty when invoked standalone "
            "(i.e. outside a running `reyn cron run` session).  Future versions will "
            "query the live scheduler via the web API."
        ),
    )
    status_p.set_defaults(func=run_status)


def _no_subcommand(args: argparse.Namespace) -> None:  # pragma: no cover
    print(
        "Usage: reyn cron <subcommand>  (run | list | status)",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_jobs() -> list:
    """Load CronJobConfig list from reyn.yaml via the standard config path."""
    from reyn.config import load_config
    config = load_config()
    return config.cron.jobs


def _jobs_to_cron_jobs(job_configs) -> list:
    """Convert CronJobConfig entries to CronJob instances.

    Both message-based (= ``to`` + ``message``) and legacy skill-based
    (= ``skill``) shapes pass through; CronJob carries all three fields
    and the runner dispatches based on ``is_message_based()``.
    """
    from reyn.cron import CronJob
    return [
        CronJob(
            name=jc.name,
            schedule=jc.schedule,
            to=jc.to,
            message=jc.message,
            skill=jc.skill,
            input=dict(jc.input),
            enabled=jc.enabled,
        )
        for jc in job_configs
    ]


def _compute_next_run(job) -> str:
    """Return ISO-format next-run time or '-' on invalid expression."""
    from reyn.cron import CronScheduler
    scheduler = CronScheduler(jobs=[job])
    next_at = scheduler.compute_next_run(job)
    return next_at.isoformat() if next_at is not None else "-"


def _print_list_table(jobs: list, *, show_last_run: bool = False) -> None:
    """Print jobs in a fixed-width tabular format."""
    if not jobs:
        print("(no jobs configured)")
        return

    # Pre-compute next_run_at for each job. FP-0041 #489 PR-B: target
    # column shows the agent (= message-based) or the skill (= legacy)
    # so operators can tell at a glance what each job dispatches.
    rows = []
    for job in jobs:
        next_str = _compute_next_run(job)
        if job.is_message_based():
            target = f"→{job.to}"
        else:
            target = job.skill or "-"
        row = {
            "name": job.name,
            "skill": target,
            "schedule": job.schedule,
            "enabled": "true" if job.enabled else "false",
            "next_run": next_str,
        }
        if show_last_run:
            row["last_run_at"] = job.last_run_at.isoformat() if job.last_run_at is not None else "-"
            row["last_run_status"] = job.last_run_status or "-"
            row["last_run_error"] = job.last_run_error or "-"
        rows.append(row)

    # Column widths
    w_name = max(len(r["name"]) for r in rows)
    w_name = max(w_name, 4)  # "NAME"
    w_skill = max(len(r["skill"]) for r in rows)
    w_skill = max(w_skill, 6)  # "TARGET"
    w_sched = max(len(r["schedule"]) for r in rows)
    w_sched = max(w_sched, 8)  # "SCHEDULE"
    w_enabled = 7  # "ENABLED"
    w_next = max(len(r["next_run"]) for r in rows)
    w_next = max(w_next, 8)  # "NEXT RUN"

    if show_last_run:
        w_lra = max(len(r["last_run_at"]) for r in rows)
        w_lra = max(w_lra, 12)  # "LAST RUN AT"
        w_lrs = max(len(r["last_run_status"]) for r in rows)
        w_lrs = max(w_lrs, 12)  # "LAST STATUS"
        w_lre = max(len(r["last_run_error"]) for r in rows)
        w_lre = max(w_lre, 10)  # "LAST ERROR"
        header = (
            f"{'NAME':<{w_name}}  {'TARGET':<{w_skill}}  "
            f"{'SCHEDULE':<{w_sched}}  {'ENABLED':<{w_enabled}}  "
            f"{'NEXT RUN':<{w_next}}  {'LAST RUN AT':<{w_lra}}  "
            f"{'LAST STATUS':<{w_lrs}}  {'LAST ERROR':<{w_lre}}"
        )
        print(header)
        print("─" * len(header))
        for r in rows:
            print(
                f"{r['name']:<{w_name}}  {r['skill']:<{w_skill}}  "
                f"{r['schedule']:<{w_sched}}  {r['enabled']:<{w_enabled}}  "
                f"{r['next_run']:<{w_next}}  {r['last_run_at']:<{w_lra}}  "
                f"{r['last_run_status']:<{w_lrs}}  {r['last_run_error']:<{w_lre}}"
            )
    else:
        header = (
            f"{'NAME':<{w_name}}  {'TARGET':<{w_skill}}  "
            f"{'SCHEDULE':<{w_sched}}  {'ENABLED':<{w_enabled}}  "
            f"{'NEXT RUN':<{w_next}}"
        )
        print(header)
        print("─" * len(header))
        for r in rows:
            print(
                f"{r['name']:<{w_name}}  {r['skill']:<{w_skill}}  "
                f"{r['schedule']:<{w_sched}}  {r['enabled']:<{w_enabled}}  "
                f"{r['next_run']:<{w_next}}"
            )


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def run_list(args: argparse.Namespace) -> None:
    """Print all configured cron jobs with next-run time."""
    job_configs = _load_jobs()
    jobs = _jobs_to_cron_jobs(job_configs)
    _print_list_table(jobs, show_last_run=False)


def run_status(args: argparse.Namespace) -> None:
    """Print cron jobs with last-run fields.

    v1 note: last_run_* fields are always empty when invoked standalone
    (outside a running ``reyn cron run`` session) because there is no
    persistent state store in v1.  This limitation is documented in
    ``docs/reference/cli/cron.md``.
    """
    job_configs = _load_jobs()
    jobs = _jobs_to_cron_jobs(job_configs)
    _print_list_table(jobs, show_last_run=True)


def run_run(args: argparse.Namespace) -> None:
    """Start the foreground cron scheduler (blocks until Ctrl-C)."""
    try:
        asyncio.run(_run_scheduler())
    except KeyboardInterrupt:
        pass  # _run_scheduler handles this internally


async def _run_scheduler() -> None:
    """Build scheduler, start jobs, block until Ctrl-C."""
    from reyn.cron import CronJob, CronScheduler

    job_configs = _load_jobs()
    jobs = _jobs_to_cron_jobs(job_configs)
    enabled = [j for j in jobs if j.enabled]

    if not jobs:
        print("No jobs configured.  Add jobs under cron.jobs in reyn.yaml.")
        return

    print(f"Started cron scheduler with {len(enabled)} enabled job(s):")
    for job in enabled:
        from reyn.cron import CronScheduler as _CS
        _sched = _CS(jobs=[job])
        next_at = _sched.compute_next_run(job)
        next_str = next_at.isoformat() if next_at is not None else "(invalid schedule)"
        print(f"  • {job.name}  ({job.schedule})  next: {next_str}")

    runner = _build_runner()
    scheduler = CronScheduler(jobs=jobs, runner_fn=runner)

    await scheduler.start()
    try:
        await asyncio.Event().wait()  # block until cancelled
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await scheduler.stop()
        print("\nCron scheduler stopped.")


def _build_runner():
    """Return the async runner function that executes a CronJob.

    FP-0009 + FP-0041 #489 PR-B: standalone CLI mode supports the
    legacy skill-based shape only (= ``inbox_pusher=None`` since there
    is no AgentRegistry context in ``reyn cron run`` foreground). Use
    ``reyn web`` for message-based jobs.
    """
    from reyn.cron.runners import build_default_runner

    async def _legacy_skill_runner(job) -> str:
        from reyn.agent import Agent
        from reyn.cli.commands.run import _build_permission_resolver
        from reyn.cli.logger_factory import make_logger
        from reyn.cli.skill_loader import resolve_skill_path
        from reyn.compiler import load_dsl_skill
        from reyn.config import _find_project_root, load_config, load_project_context
        from reyn.llm.model_resolver import ModelResolver
        from reyn.user_intervention import StdinInterventionBus

        config = load_config()
        model_class = config.model
        resolver = ModelResolver(config.models)
        resolved = resolver.resolve(model_class).model
        project_root = _find_project_root(Path.cwd())
        project_context = load_project_context(config, project_root)
        logger = make_logger()

        # #997 dir2: config-derived permission/runtime bundle wired by
        # Agent.from_config (cron jobs run unattended → shell_allowed=False).
        agent = Agent.from_config(
            config,
            shell_allowed=False,
            model=resolved,
            resolver=resolver,
            strict=False,
            subscribers=[logger],
            intervention_bus=StdinInterventionBus(),
            project_context=project_context,
            caller="cron",
        )

        skill_dir, skill_root = resolve_skill_path(job.skill)
        skill_md = skill_dir / "skill.md"
        skill = load_dsl_skill(str(skill_md), skill_root=str(skill_root))
        initial_input = job.input if job.input else {}

        result = await agent.run(skill, initial_input)
        if not result.ok:
            raise RuntimeError(
                f"Skill {job.skill!r} ended with status {result.status!r}"
            )
        return "ok"

    # Standalone CLI has no AgentRegistry → message-based jobs warn
    # and skip (= operator should use ``reyn web`` for them).
    return build_default_runner(
        legacy_skill_runner=_legacy_skill_runner,
        inbox_pusher=None,
    )
