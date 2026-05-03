"""/skill slash command — manage active skill runs (PR-resume-ux U2).

Sub-commands:
  /skill list                 — show active skill runs
  /skill discard <run_id>     — abort a specific run + cleanup

Note: distinct from ``/skills`` (plural, PR-tui-4) which lists *available*
skills (catalogue). This one targets *running* skill instances.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from reyn.chat.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession


_USAGE = (
    "Usage: /skill <list|discard <run_id>>\n"
    "  list                — show active skill runs\n"
    "  discard <run_id>    — abort a specific run"
)


@slash("skill", summary="Manage active skill runs (list / discard)")
async def skill_cmd(session: "ChatSession", args: str) -> None:
    """Dispatch to sub-command based on the first argument."""
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await reply(session, _USAGE)
        return
    sub = parts[0]
    sub_args = parts[1] if len(parts) > 1 else ""
    if sub == "list":
        await _list_skill_runs(session)
    elif sub == "discard":
        await _discard_skill_run(session, sub_args)
    else:
        await reply_error(session, _USAGE)


async def _list_skill_runs(session: "ChatSession") -> None:
    """Emit a status message listing active skill runs."""
    reg = session._get_skill_registry()
    if reg is None:
        await reply(session, "(no active skills — state log not configured)")
        return
    run_ids = reg.list_active()
    if not run_ids:
        await reply(session, "(no active skills)")
        return
    lines = [f"{len(run_ids)} active skill run(s):"]
    for run_id in run_ids:
        snap = reg.get(run_id)
        if snap is None:
            lines.append(f"  {run_id}  (snapshot missing)")
            continue
        phase = snap.current_phase or "(unknown)"
        lines.append(f"  {run_id}  {snap.skill_name}  @ {phase}")
    await reply(session, "\n".join(lines))


async def _discard_skill_run(session: "ChatSession", args: str) -> None:
    """Abort the run: cancel task, drop interventions, mark discarded."""
    run_id = args.strip()
    if not run_id:
        await reply_error(session, "Usage: /skill discard <run_id>")
        return
    reg = session._get_skill_registry()
    if reg is None:
        await reply_error(session, "skill registry not available")
        return
    if reg.get(run_id) is None:
        await reply_error(session, f"unknown skill run: {run_id}")
        return

    # 1. Cancel the asyncio.Task if mid-session
    task = session.running_skills.get(run_id)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # Cancellation is expected; other exceptions during cleanup are
            # captured here to avoid blocking the discard path.
            pass
        session.running_skills.pop(run_id, None)
        session.running_skills_started_at.pop(run_id, None)

    # 2. Cancel any pending interventions for this run
    session._drop_interventions_for_run(run_id)

    # 3. Mark as discarded (WAL append + per-skill snapshot unlink)
    await reg.complete(run_id=run_id, status="discarded")

    await reply(session, f"discarded skill run: {run_id}")
