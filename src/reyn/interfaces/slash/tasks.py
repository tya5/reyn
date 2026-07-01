"""/tasks slash command — unified async-task view (FP-0012 Component D).

Spans both async-task families in one view (#2035, restoring the FP-0012
"unified" intent after the planner deletion #2018):
  - **skill runs** — long-running ``@sub_skill`` / ``run_skill`` executions,
    including crash-recovery auto-resumed runs (``AutoResumeHandler`` re-spawns
    in-flight skill_runs on session start via ``spawn_resumed_skill``).
  - **dynamic tasks** — the Tasks the chat LLM creates via ``task__create``
    (#2026/#2028/#2034), tracked in the session-scoped Task backend.

Sub-commands:
  /tasks                          — list running skill runs + dynamic tasks
  /tasks list                     — same as `/tasks`
  /tasks status <run_id_prefix>   — show current phase + elapsed time + last event
  /tasks kill   <run_id_prefix>   — cancel a specific task

Reads from existing infrastructure (= no new state required):
  - ``session.running_skills`` / ``running_skills_started_at`` /
    ``running_skills_chain`` for skill runs (PR22), including auto-resumed runs
  - SkillRegistry per-skill snapshot for current_phase
  - ``session.task_backend`` for dynamic tasks (#1953 slice R); ``None`` when
    the session carries no backend, in which case the dynamic section is empty.
  - The P6 events log via ``self._chat_events`` is NOT consulted here (= keeping
    the slash cheap and synchronous; users wanting raw events run ``reyn events``).

The legacy ``/skill list`` and ``/skill discard`` commands continue to work as
before (= unchanged); ``/tasks`` is the entry point for running skill runs.
This mirrors the FP-0012 proposal's Component D.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session


_USAGE = (
    "Usage: /tasks [list|status <run_id>|kill <run_id>]\n"
    "  list              — show running skill runs + dynamic tasks. Default.\n"
    "  status <prefix>   — show current phase + elapsed for a specific task\n"
    "  kill   <prefix>   — cancel a specific task"
)


@slash(
    "tasks",
    summary="Unified view of running async tasks (skill runs + dynamic tasks)",
    usage="/tasks [list|status <run_id>|kill <run_id>]",
)
async def tasks_cmd(session: "Session", args: str) -> None:
    parts = args.strip().split(maxsplit=1)
    if not parts or parts[0] == "list":
        await _list_tasks(session)
        return
    sub = parts[0]
    sub_args = parts[1] if len(parts) > 1 else ""
    if sub == "status":
        await _task_status(session, sub_args)
    elif sub == "kill":
        await _kill_task(session, sub_args)
    else:
        await reply_error(session, _USAGE)


# ── helpers ──────────────────────────────────────────────────────────────────


def _format_elapsed(seconds: float) -> str:
    """Render a monotonic duration in a human-friendly form."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"


async def _resolve_task(
    session: "Session", prefix: str,
) -> tuple[str | None, str, list[str]]:
    """Resolve a run_id / task_id from a prefix across both task families.

    Returns ``(resolved_id, kind, candidates)``:
      - ``resolved_id`` is non-None iff exactly one match exists across BOTH
        skill runs and dynamic tasks.
      - ``kind`` is "skill" or "task" or "" (when ambiguous / unmatched).
      - ``candidates`` lists every match (``skill:<id>`` / ``task:<id>``) so the
        caller can show a "did you mean?" hint.
    """
    prefix = prefix.strip()
    if not prefix:
        return None, "", []
    skill_ids = list(getattr(session, "running_skills", {}).keys())
    skill_matches = [r for r in skill_ids if prefix in r]
    # Dynamic tasks: /tasks (#2036) LISTS them, so status|kill must resolve the
    # same ids the list shows — otherwise the user sees a task they can't act on
    # (the list-vs-action gap this fixes).
    task_matches: list[str] = []
    backend = getattr(session, "task_backend", None)
    if backend is not None:
        try:
            task_matches = [
                t.task_id for t in await backend.list() if prefix in t.task_id
            ]
        except Exception:
            task_matches = []
    candidates = (
        [f"skill:{r}" for r in skill_matches]
        + [f"task:{t}" for t in task_matches]
    )
    if len(candidates) == 1:
        if skill_matches:
            return skill_matches[0], "skill", candidates
        return task_matches[0], "task", candidates
    return None, "", candidates


# ── /tasks list ──────────────────────────────────────────────────────────────


async def _list_tasks(session: "Session") -> None:
    skill_lines = _list_skill_lines(session)
    task_lines = await _list_dynamic_task_lines(session)
    if not skill_lines and not task_lines:
        await reply(session, "(no running tasks)")
        return

    # "task(s)" not "running task(s)": the Tasks section now shows the full plan
    # incl completed (#2036), so the count spans running skill runs + persistent
    # tasks of any non-archived status.
    total = len(skill_lines) + len(task_lines)
    out: list[str] = [f"{total} task(s):"]
    if skill_lines:
        out.append("  Skills:")
        out.extend(f"    {ln}" for ln in skill_lines)
    if task_lines:
        out.append("  Tasks:")
        out.extend(f"    {ln}" for ln in task_lines)
    await reply(session, "\n".join(out))


def _list_skill_lines(session: "Session") -> list[str]:
    running = getattr(session, "running_skills", {}) or {}
    if not running:
        return []
    started_at = getattr(session, "running_skills_started_at", {}) or {}
    reg = session.get_skill_registry()
    now = time.monotonic()
    lines: list[str] = []
    for run_id in running.keys():
        elapsed = now - started_at.get(run_id, now)
        skill_label = run_id
        phase = "(unknown)"
        if reg is not None:
            snap = reg.get(run_id)
            if snap is not None:
                skill_label = snap.skill_name or run_id
                phase = snap.current_phase or "(starting)"
        lines.append(
            f"{skill_label}  [{run_id}]  {_format_elapsed(elapsed)}  "
            f"phase: {phase}"
        )
    return lines


# Dynamic Tasks are PERSISTENT trackable work-units (the point of the
# dynamic-task model), so the Tasks section shows the FULL plan WITH status —
# active + completed + failed — so the user sees progress ("3/6 done") and deps
# referencing completed tasks stay intact. Only SOFT-DELETED tasks (``archived_at``
# set — abort dismisses a task by setting it alongside the ABORTED lifecycle state,
# #2187) are hidden. NB the skill-runs section keeps its own running-only filter
# (ephemeral runs, not trackable plans = different semantics); the split is
# intentional (#2036 review).


async def _list_dynamic_task_lines(session: "Session") -> list[str]:
    """Render the dynamic Tasks (``task__create`` work-units) for /tasks.

    Reads ``session.task_backend`` (#1953 slice R). Returns ``[]`` when the
    session carries no backend. Shows the full plan WITH status (active +
    completed + failed) so the user sees progress + intact deps; only
    SOFT-DELETED tasks (``archived_at`` set, #2187) are hidden. Each task is
    formatted in the same idiom as ``_list_skill_lines``.
    """
    backend = getattr(session, "task_backend", None)
    if backend is None:
        return []
    tasks = await backend.list()
    lines: list[str] = []
    for task in tasks:
        status = getattr(task.status, "value", task.status)
        if getattr(task, "archived_at", None) is not None:  # soft-deleted (retention) — hidden
            continue
        deps = list(getattr(task, "deps", []) or [])
        if deps:
            deps_summary = ", ".join(d[:8] for d in deps)
        else:
            deps_summary = "(none)"
        lines.append(
            f"{task.name}  [{task.task_id[:8]}]  status: {status}  "
            f"deps: {deps_summary}"
        )
    return lines


# ── /tasks status ────────────────────────────────────────────────────────────


async def _task_status(session: "Session", args: str) -> None:
    prefix = args.strip()
    if not prefix:
        await reply_error(session, "Usage: /tasks status <run_id_prefix>")
        return
    resolved, kind, candidates = await _resolve_task(session, prefix)
    if resolved is None:
        if not candidates:
            await reply_error(session, f"no task matches {prefix!r}")
        else:
            await reply_error(
                session,
                f"ambiguous prefix {prefix!r}; matches: {', '.join(candidates)}",
            )
        return

    if kind == "skill":
        await _skill_status(session, resolved)
    elif kind == "task":
        await _dynamic_task_status(session, resolved)
    else:
        await reply_error(session, f"unknown task kind for {resolved}")


async def _dynamic_task_status(session: "Session", task_id: str) -> None:
    """Render a dynamic task's state for /tasks status (#2036 follow-up)."""
    backend = getattr(session, "task_backend", None)
    task = await backend.get(task_id) if backend is not None else None
    if task is None:
        await reply_error(session, f"task {task_id} not found")
        return
    status = getattr(task.status, "value", task.status)
    deps = list(getattr(task, "deps", []) or [])
    out: list[str] = [
        f"task {task.task_id}",
        f"  name:    {task.name}",
        f"  status:  {status}",
        f"  deps:    {', '.join(d[:8] for d in deps) if deps else '(none)'}",
    ]
    if getattr(task, "description", None):
        out.append(f"  detail:  {task.description}")
    if getattr(task, "assignee", None):
        out.append(f"  assignee: {task.assignee}")
    if getattr(task, "result", None):
        out.append(f"  result:  {task.result}")
    await reply(session, "\n".join(out))


async def _skill_status(session: "Session", run_id: str) -> None:
    started_at = getattr(session, "running_skills_started_at", {}) or {}
    elapsed = time.monotonic() - started_at.get(run_id, time.monotonic())
    reg = session.get_skill_registry()
    out: list[str] = [
        f"skill run {run_id}",
        f"  elapsed:  {_format_elapsed(elapsed)}",
    ]
    if reg is not None:
        snap = reg.get(run_id)
        if snap is not None:
            out.append(f"  skill:    {snap.skill_name or '(unknown)'}")
            out.append(f"  phase:    {snap.current_phase or '(starting)'}")
            parent = getattr(snap, "parent_run_id", None)
            if parent:
                out.append(f"  parent:   {parent}")
    chain_id = (getattr(session, "running_skills_chain", {}) or {}).get(run_id)
    if chain_id:
        out.append(f"  chain_id: {chain_id}")
    await reply(session, "\n".join(out))


# ── /tasks kill ──────────────────────────────────────────────────────────────


async def _kill_task(session: "Session", args: str) -> None:
    prefix = args.strip()
    if not prefix:
        await reply_error(session, "Usage: /tasks kill <run_id_prefix>")
        return
    resolved, kind, candidates = await _resolve_task(session, prefix)
    if resolved is None:
        if not candidates:
            await reply_error(session, f"no task matches {prefix!r}")
        else:
            await reply_error(
                session,
                f"ambiguous prefix {prefix!r}; matches: {', '.join(candidates)}",
            )
        return

    if kind == "skill":
        # Reuse the existing /skill discard implementation rather than
        # duplicating cancel+notify+complete logic. Lazy-import to avoid
        # circular imports at module load time.
        # Pass --force: /tasks kill is an explicit intent — skip the
        # two-step confirmation that /skill discard (without --force) shows.
        from reyn.interfaces.slash.skill import _discard_skill_run
        await _discard_skill_run(session, f"{resolved} --force")
    elif kind == "task":
        # A dynamic task: abort it via the backend (#2036 follow-up). abort()
        # transitions the task + its dependents out of the runnable set.
        backend = getattr(session, "task_backend", None)
        if backend is None:
            await reply_error(session, f"no task backend; cannot kill {resolved}")
            return
        await backend.abort(resolved, reason="/tasks kill")
        await reply(session, f"aborted task {resolved}")
    else:
        await reply_error(session, f"unknown task kind for {resolved}")
