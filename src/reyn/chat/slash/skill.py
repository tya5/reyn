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
    """Emit a status message listing active skill runs.

    R-D13: when a child run was spawned via ``run_skill`` and the parent
    is still active, the display walks the parent_run_id chain to show
    the lineage as ``parent_skill / child_skill``. Roots (no parent)
    show ``skill_name`` only. Orphaned children (parent not in active
    list) display as roots — the parent has either completed or this
    is a stale snapshot.
    """
    reg = session._get_skill_registry()
    if reg is None:
        await reply(session, "(no active skills — state log not configured)")
        return
    run_ids = reg.list_active()
    if not run_ids:
        await reply(session, "(no active skills)")
        return
    # Build a lookup of run_id → snapshot for parent walks. Skipping
    # missing snapshots keeps the display robust when a child outlives
    # its parent's snapshot file (= parent completed, child still running).
    by_run: dict = {}
    for rid in run_ids:
        snap = reg.get(rid)
        if snap is not None:
            by_run[rid] = snap

    def _lineage_label(snap) -> str:
        """Walk parent_run_id chain, return ``A_skill / B_skill / C_skill``.

        Defensive against cycles (cap at 8 hops) — though SkillRegistry
        creates IDs uniquely so cycles shouldn't form in practice.
        """
        parts = [snap.skill_name or "?"]
        seen = {snap.skill_run_id}
        cur = snap
        for _ in range(8):
            parent_id = getattr(cur, "parent_run_id", None)
            if not parent_id or parent_id in seen:
                break
            parent_snap = by_run.get(parent_id)
            if parent_snap is None:
                break
            seen.add(parent_id)
            parts.insert(0, parent_snap.skill_name or "?")
            cur = parent_snap
        return " / ".join(parts)

    lines = [f"{len(run_ids)} active skill run(s):"]
    for run_id in run_ids:
        snap = by_run.get(run_id)
        if snap is None:
            lines.append(f"  {run_id}  (snapshot missing)")
            continue
        phase = snap.current_phase or "(unknown)"
        lines.append(f"  {run_id}  {_lineage_label(snap)}  @ {phase}")
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

    # 4. R-D14: notify the upstream chain waiter (if any) so they don't
    # stay stuck for chain_timeout_seconds. Look up the chain_id we
    # stashed when this skill_run was spawned; if present, the
    # AgentRegistry's notify_chain_discarded scans every other agent's
    # ChainManager and force-resolves the matching pending chain.
    chain_id = session.running_skills_chain.pop(run_id, None)
    if chain_id and getattr(session, "_registry", None) is not None:
        try:
            await session._registry.notify_chain_discarded(
                chain_id=chain_id,
                by_agent_name=session.agent_name,
                reason="user_discarded_skill_run",
            )
        except Exception:  # noqa: BLE001 — defensive; discard succeeds anyway
            import logging
            logging.getLogger(__name__).warning(
                "notify_chain_discarded failed for chain %s", chain_id,
                exc_info=True,
            )

    await reply(session, f"discarded skill run: {run_id}")
