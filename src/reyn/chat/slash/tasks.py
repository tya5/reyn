"""/tasks slash command — unified async-task view (FP-0012 Component D).

Sub-commands:
  /tasks                          — list all running tasks (skills + plans)
  /tasks list                     — same as `/tasks`
  /tasks status <run_id_prefix>   — show current phase + elapsed time + last event
  /tasks kill   <run_id_prefix>   — cancel a specific task

Reads from existing infrastructure (= no new state required):
  - ``session.running_skills`` / ``running_skills_started_at`` /
    ``running_skills_chain`` for skill runs (PR22)
  - ``session.running_plans`` + ``active_plan_ids`` for plan tasks (ADR-0023
    Phase 2.1)
  - SkillRegistry per-skill snapshot for current_phase
  - The P6 events log via ``self._chat_events`` is NOT consulted here (= keeping
    the slash cheap and synchronous; users wanting raw events run ``reyn events``).

The legacy ``/skill list`` and ``/skill discard`` commands continue to work as
before (= unchanged); ``/tasks`` is the unified entry point spanning both
skill runs and plan tasks. This mirrors the FP-0012 proposal's Component D.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from reyn.chat.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession


_USAGE = (
    "Usage: /tasks [list|status <run_id>|kill <run_id>]\n"
    "  list              — show all running tasks (skills + plans). Default.\n"
    "  status <prefix>   — show current phase + elapsed for a specific task\n"
    "  kill   <prefix>   — cancel a specific task"
)


@slash("tasks", summary="Unified view of running async tasks (skills + plans)")
async def tasks_cmd(session: "ChatSession", args: str) -> None:
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


def _resolve_task(
    session: "ChatSession", prefix: str,
) -> tuple[str | None, str, list[str]]:
    """Resolve a run_id / plan_id from a prefix.

    Returns ``(resolved_id, kind, candidates)``:
      - ``resolved_id`` is non-None iff exactly one match exists.
      - ``kind`` is "skill" or "plan" or "" (when ambiguous / unmatched).
      - ``candidates`` lists every match across both task families so the
        caller can show a "did you mean?" hint.
    """
    prefix = prefix.strip()
    if not prefix:
        return None, "", []
    skill_ids = list(getattr(session, "running_skills", {}).keys())
    plan_ids: list[str] = []
    plans_dict = getattr(session, "running_plans", None)
    if plans_dict is not None:
        plan_ids = list(plans_dict.keys())
    skill_matches = [r for r in skill_ids if prefix in r]
    plan_matches = [p for p in plan_ids if prefix in p]
    candidates = [f"skill:{r}" for r in skill_matches] + [
        f"plan:{p}" for p in plan_matches
    ]
    if len(candidates) == 1:
        if skill_matches:
            return skill_matches[0], "skill", candidates
        return plan_matches[0], "plan", candidates
    return None, "", candidates


# ── /tasks list ──────────────────────────────────────────────────────────────


async def _list_tasks(session: "ChatSession") -> None:
    skill_lines = _list_skill_lines(session)
    plan_lines = _list_plan_lines(session)
    if not skill_lines and not plan_lines:
        await reply(session, "(no running tasks)")
        return

    out: list[str] = []
    total = len(skill_lines) + len(plan_lines)
    out.append(f"{total} running task(s):")
    if skill_lines:
        out.append("  Skills:")
        out.extend(f"    {ln}" for ln in skill_lines)
    if plan_lines:
        out.append("  Plans:")
        out.extend(f"    {ln}" for ln in plan_lines)
    await reply(session, "\n".join(out))


def _list_skill_lines(session: "ChatSession") -> list[str]:
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


def _list_plan_lines(session: "ChatSession") -> list[str]:
    running = getattr(session, "running_plans", None)
    if not running:
        return []
    lines: list[str] = []
    for plan_id, task in running.items():
        if task.done():
            # Skip plans whose task has finished but whose entry hasn't been
            # cleaned up yet (= terminal state, no longer "running").
            continue
        lines.append(f"plan  [{plan_id}]")
    return lines


# ── /tasks status ────────────────────────────────────────────────────────────


async def _task_status(session: "ChatSession", args: str) -> None:
    prefix = args.strip()
    if not prefix:
        await reply_error(session, "Usage: /tasks status <run_id_prefix>")
        return
    resolved, kind, candidates = _resolve_task(session, prefix)
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
    elif kind == "plan":
        await _plan_status(session, resolved)
    else:
        await reply_error(session, f"unknown task kind for {resolved}")


async def _skill_status(session: "ChatSession", run_id: str) -> None:
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


async def _plan_status(session: "ChatSession", plan_id: str) -> None:
    out = [f"plan {plan_id}", "  (use `/plan list` for the running plan view)"]
    await reply(session, "\n".join(out))


# ── /tasks kill ──────────────────────────────────────────────────────────────


async def _kill_task(session: "ChatSession", args: str) -> None:
    prefix = args.strip()
    if not prefix:
        await reply_error(session, "Usage: /tasks kill <run_id_prefix>")
        return
    resolved, kind, candidates = _resolve_task(session, prefix)
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
        from reyn.chat.slash.skill import _discard_skill_run
        await _discard_skill_run(session, resolved)
    elif kind == "plan":
        # Mirror /plan discard semantics — cancel the task; the plan's
        # registered handler clears active_plan_ids on cleanup.
        running_plans = getattr(session, "running_plans", {}) or {}
        task = running_plans.get(resolved)
        if task is None or task.done():
            await reply_error(session, f"plan {resolved} is not running")
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await reply(session, f"killed plan: {resolved}")
    else:
        await reply_error(session, f"unknown task kind for {resolved}")
