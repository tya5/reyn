"""/plan slash command — manage active plan-mode runs (ADR-0023 Phase 2.1).

Sub-commands:
  /plan list                 — show active plan runs
  /plan discard <plan_id>    — abort a specific plan run + cleanup

Mirrors ``/skill`` (skill.py) shape. Distinct from any future ``/plans``
command (= would list available plan templates if such a concept were
to be introduced; not in scope today).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from reyn.chat.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession


_USAGE = (
    "Usage: /plan <list|discard <plan_id>>\n"
    "  list                 — show active plan runs\n"
    "  discard <plan_id>    — abort a specific plan run"
)


@slash("plan", summary="Manage active plan-mode runs (list / discard)")
async def plan_cmd(session: "ChatSession", args: str) -> None:
    """Dispatch to sub-command based on the first argument."""
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await reply(session, _USAGE)
        return
    sub = parts[0]
    sub_args = parts[1] if len(parts) > 1 else ""
    if sub == "list":
        await _list_plan_runs(session)
    elif sub == "discard":
        await _discard_plan_run(session, sub_args)
    else:
        await reply_error(session, _USAGE)


async def _list_plan_runs(session: "ChatSession") -> None:
    """Emit a status message listing active plan runs.

    Reads from session.running_plans (= the asyncio.Task tracking dict
    populated by spawn_plan_task / _spawn_resumed_plan). Cross-references
    with active_plan_ids from the agent snapshot for any plans that
    survived a crash but haven't been adopted by a running task yet.
    """
    running = dict(session.running_plans)
    snap = session._journal.snapshot
    active_ids = list(snap.active_plan_ids)

    # Combined view: tasks currently running (in-process) + plan_ids
    # in the snapshot (durable). A plan_id may appear in active_ids
    # without a running task during the brief window between crash and
    # _spawn_resumed_plan firing.
    combined: dict[str, str] = {}  # plan_id -> status
    for plan_id, task in running.items():
        if task.done():
            combined[plan_id] = "done (cleanup pending)"
        else:
            combined[plan_id] = "running"
    for plan_id in active_ids:
        if plan_id not in combined:
            combined[plan_id] = "active (no task — resume pending)"

    if not combined:
        await reply(session, "(no active plans)")
        return

    lines = [f"{len(combined)} active plan run(s):"]
    for plan_id, status in combined.items():
        lines.append(f"  {plan_id}  {status}")
    await reply(session, "\n".join(lines))


async def _discard_plan_run(session: "ChatSession", args: str) -> None:
    """Abort a plan: cancel task, surface outbox notice, clean up state.

    Steps (mirror /skill discard):
      1. Cancel asyncio.Task (= triggers PlanRuntime's finally clause →
         no plan_completed → active_plan_ids preserved for cleanup).
      2. Record plan_aborted in WAL (= clears active_plan_ids).
      3. Delete decomposition artifact + plan snapshot via
         delete_plan_decomposition (= P5 cleanup).
      4. R-D14 cross-agent notify so any peer waiting on the plan's
         per-plan chain_id (= f"plan_{plan_id}") gets resolved
         immediately rather than waiting for chain_timeout.
    """
    plan_id = args.strip()
    if not plan_id:
        await reply_error(session, "Usage: /plan discard <plan_id>")
        return

    # Existence check across both running_plans and active_plan_ids.
    snap = session._journal.snapshot
    is_running = plan_id in session.running_plans
    is_active = plan_id in snap.active_plan_ids
    if not is_running and not is_active:
        await reply_error(session, f"unknown plan run: {plan_id}")
        return

    # 1. Cancel the asyncio.Task if mid-flight.
    task = session.running_plans.get(plan_id)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        session.running_plans.pop(plan_id, None)

    # 2. Record plan_aborted (= prunes active_plan_ids, surfaces audit).
    try:
        await session._journal.record_plan_aborted(
            plan_id=plan_id, reason="user_discarded",
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "plan_aborted on /plan discard failed for %s: %r",
            plan_id, exc,
        )

    # 3. Delete decomposition artifact (= P5 cleanup).
    try:
        await session.delete_plan_decomposition(plan_id=plan_id)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "delete_plan_decomposition on /plan discard failed for %s: %r",
            plan_id, exc,
        )

    # Also delete the per-plan snapshot file if it exists (= rehydrated
    # on a previous restart but never adopted by _spawn_resumed_plan).
    try:
        from pathlib import Path
        from reyn.plan.plan_snapshot import plan_snapshot_path
        agent_state_dir = (
            Path(".reyn") / "agents" / session.agent_name / "state"
        )
        snap_path = plan_snapshot_path(agent_state_dir, plan_id)
        snap_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — defensive
        import logging
        logging.getLogger(__name__).warning(
            "plan snapshot cleanup on /plan discard failed for %s: %r",
            plan_id, exc,
        )

    # 4. R-D14 cross-agent notify on the per-plan chain_id (= ADR-0023
    # §2.1.2). Any peer agent waiting on f"plan_{plan_id}" gets resolved
    # immediately rather than waiting for chain_timeout_seconds.
    plan_chain_id = f"plan_{plan_id}"
    if getattr(session, "_registry", None) is not None:
        try:
            await session._registry.notify_chain_discarded(
                chain_id=plan_chain_id,
                by_agent_name=session.agent_name,
                reason="user_discarded_plan",
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "notify_chain_discarded failed for plan chain %s",
                plan_chain_id, exc_info=True,
            )

    await reply(session, f"discarded plan run: {plan_id}")
