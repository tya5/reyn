"""/tasks slash command — dynamic async-task view (FP-0012 Component D).

Lists the Tasks the chat LLM creates via ``task__create``
(#2026/#2028/#2034), tracked in the session-scoped Task backend.

Sub-commands:
  /tasks                          — list dynamic tasks
  /tasks list                     — same as `/tasks`
  /tasks status <task_id_prefix>  — show a task's status + deps
  /tasks kill   <task_id_prefix>  — abort a specific task

Reads from existing infrastructure (= no new state required):
  - ``session.task_backend`` for dynamic tasks (#1953 slice R); ``None`` when
    the session carries no backend, in which case the view is empty.
  - The P6 events log via ``self._chat_events`` is NOT consulted here (= keeping
    the slash cheap and synchronous; users wanting raw events run ``reyn events``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session


_USAGE = (
    "Usage: /tasks [list|status <task_id>|kill <task_id>]\n"
    "  list              — show dynamic tasks. Default.\n"
    "  status <prefix>   — show status + deps for a specific task\n"
    "  kill   <prefix>   — cancel a specific task"
)


@slash(
    "tasks",
    summary="View of running dynamic tasks",
    usage="/tasks [list|status <task_id>|kill <task_id>]",
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


async def _resolve_task(
    session: "Session", prefix: str,
) -> tuple[str | None, list[str]]:
    """Resolve a task_id from a prefix across dynamic tasks.

    Returns ``(resolved_id, candidates)``:
      - ``resolved_id`` is non-None iff exactly one match exists.
      - ``candidates`` lists every match (``task:<id>``) so the caller can
        show a "did you mean?" hint.
    """
    prefix = prefix.strip()
    if not prefix:
        return None, []
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
    candidates = [f"task:{t}" for t in task_matches]
    if len(candidates) == 1:
        return task_matches[0], candidates
    return None, candidates


# ── /tasks list ──────────────────────────────────────────────────────────────


async def _list_tasks(session: "Session") -> None:
    task_lines, done_count = await _list_dynamic_task_lines(session)
    if not task_lines and not done_count:
        await reply(session, "(no running tasks)")
        return

    out: list[str] = []
    if task_lines:
        out.append(f"{len(task_lines)} task(s):")
    out.append("  Tasks:")
    out.extend(f"    {ln}" for ln in task_lines)
    if done_count:
        out.append(f"    +{done_count} done")
    await reply(session, "\n".join(out))


# Dynamic Tasks are PERSISTENT trackable work-units (the point of the
# dynamic-task model), so the Tasks section shows the FULL plan WITH status —
# active + completed + failed — so the user sees progress ("3/6 done") and deps
# referencing completed tasks stay intact. Only SOFT-DELETED tasks (``archived_at``
# set — abort dismisses a task by setting it alongside the ABORTED lifecycle state,
# #2187) are hidden.


async def _list_dynamic_task_lines(session: "Session") -> tuple[list[str], int]:
    """Render the dynamic Tasks (``task__create`` work-units) for /tasks.

    Reads ``session.task_backend`` (#1953 slice R). Returns ``([], 0)`` when
    the session carries no backend. Active tasks (non-DONE, non-archived) are
    returned as formatted lines; DONE tasks are folded into a count so a long
    session does not accumulate stale completed-task clutter (#2040). Only
    SOFT-DELETED tasks (``archived_at`` set, #2187) are hidden entirely.
    """
    backend = getattr(session, "task_backend", None)
    if backend is None:
        return [], 0
    tasks = await backend.list()
    lines: list[str] = []
    done_count = 0
    for task in tasks:
        status = getattr(task.status, "value", task.status)
        if getattr(task, "archived_at", None) is not None:  # soft-deleted (retention) — hidden
            continue
        if status == "done":
            done_count += 1
            continue
        deps = list(getattr(task, "deps", []) or [])
        deps_summary = ", ".join(d[:8] for d in deps) if deps else "(none)"
        lines.append(
            f"{task.name}  [{task.task_id[:8]}]  status: {status}  "
            f"deps: {deps_summary}"
        )
    return lines, done_count


# ── /tasks status ────────────────────────────────────────────────────────────


async def _task_status(session: "Session", args: str) -> None:
    prefix = args.strip()
    if not prefix:
        await reply_error(session, "Usage: /tasks status <task_id_prefix>")
        return
    resolved, candidates = await _resolve_task(session, prefix)
    if resolved is None:
        if not candidates:
            await reply_error(session, f"no task matches {prefix!r}")
        else:
            await reply_error(
                session,
                f"ambiguous prefix {prefix!r}; matches: {', '.join(candidates)}",
            )
        return
    await _dynamic_task_status(session, resolved)


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


# ── /tasks kill ──────────────────────────────────────────────────────────────


async def _kill_task(session: "Session", args: str) -> None:
    prefix = args.strip()
    if not prefix:
        await reply_error(session, "Usage: /tasks kill <task_id_prefix>")
        return
    resolved, candidates = await _resolve_task(session, prefix)
    if resolved is None:
        if not candidates:
            await reply_error(session, f"no task matches {prefix!r}")
        else:
            await reply_error(
                session,
                f"ambiguous prefix {prefix!r}; matches: {', '.join(candidates)}",
            )
        return
    # A dynamic task: abort it via the backend (#2036 follow-up). abort()
    # transitions the task + its dependents out of the runnable set.
    backend = getattr(session, "task_backend", None)
    if backend is None:
        await reply_error(session, f"no task backend; cannot kill {resolved}")
        return
    await backend.abort(resolved, reason="/tasks kill")
    await reply(session, f"aborted task {resolved}")
