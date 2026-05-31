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
    "Usage: /plan <list|discard <plan_id>|resume <plan_id> --from <step_id>>\n"
    "  list                                  — show active plan runs\n"
    "  discard <plan_id>                     — abort a specific plan run\n"
    "  resume <plan_id> --from <step_id>     — re-run from a specific step\n"
    "                                          (ADR-0023 §3.7 escape hatch)"
)


def _plan_completer(session: "ChatSession", arg_partial: str = "") -> list[str]:
    """Surface plan IDs or step IDs for the ``/plan`` arg the user is typing.

    Two contexts handled:

    * ``/plan discard <pid_partial>`` and ``/plan resume <pid_partial>``
      → return active plan IDs from ``session.running_plans``.
    * ``/plan resume <plan_id> --from <step_partial>`` → return step IDs
      from the named plan's decomposition artifact.

    Returns an empty list (= fall back to plain hint mode) when none of
    the patterns above match (``list`` subcommand, no args yet, or the
    session / artifact isn't readable for any reason).
    """
    parts = arg_partial.split()
    sub = parts[0] if parts else ""
    if sub == "discard":
        try:
            return list(session.running_plans.keys())
        except Exception:
            return []
    if sub == "resume":
        # Past ``--from`` → step_id context; before it → plan_id context.
        if "--from" in parts[1:]:
            try:
                plan_id = parts[1]
                return _step_ids_for_plan(session, plan_id)
            except Exception:
                return []
        try:
            return list(session.running_plans.keys())
        except Exception:
            return []
    return []


def _step_ids_for_plan(
    session: "ChatSession", plan_id: str,
) -> list[str]:
    """Load the decomposition artifact for ``plan_id`` and return step IDs.

    Returns an empty list on any failure (= no such plan, artifact
    missing, read error) so a broken / stale plan_id falls back to
    plain hint mode rather than breaking the picker.
    """
    from pathlib import Path

    from reyn.plan import read_decomposition

    try:
        agent_state_dir = (
            Path(".reyn") / "agents" / session.agent_name / "state"
        )
        decomposition = read_decomposition(agent_state_dir, plan_id)
        return [s.id for s in decomposition.steps]
    except Exception:
        return []


@slash(
    "plan",
    summary="Manage active plan-mode runs",
    usage="/plan [list|discard <id>|resume <id>]",
    completer=_plan_completer,
    see_also=("docs/concepts/multi-agent/plan-mode.md",),
)
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
    elif sub == "resume":
        await _resume_from_step(session, sub_args)
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

    Two-step confirm pattern (mirrors ``/reset``): first invocation prints
    a warning showing the plan summary and asks for ``/plan discard <id>
    confirm``; second invocation (args ending with " confirm") proceeds.

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
    stripped = args.strip()
    # Detect "confirm" suffix (case-insensitive, space-separated).
    if stripped.lower().endswith(" confirm"):
        plan_id = stripped[: -len(" confirm")].strip()
        _do_confirm = True
    else:
        plan_id = stripped
        _do_confirm = False

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

    if not _do_confirm:
        # First invocation — show warning with plan context, require confirm.
        status = "running" if is_running else "active (no task)"
        # Try to surface step count from the decomposition artifact.
        step_hint = ""
        try:
            from pathlib import Path

            from reyn.plan import read_decomposition
            agent_state_dir = (
                Path(".reyn") / "agents" / session.agent_name / "state"
            )
            decomp = read_decomposition(agent_state_dir, plan_id)
            n = len(decomp.steps)
            step_hint = f", {n} step{'s' if n != 1 else ''}"
        except Exception:  # noqa: BLE001 — best-effort
            pass
        await reply(
            session,
            f"⚠ About to discard plan: {plan_id} ({status}{step_hint})\n"
            f"Type `/plan discard {plan_id} confirm` to proceed, "
            "or anything else to leave it running.",
        )
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

    # 1.5. Emit plan_aborted system outbox so TUI subscribers (=
    # AsyncStackPanel, PR #532) can clear their plan row. Placed
    # before the journal record / chain notify so later steps
    # raising doesn't leave the bottom-strip stale (#536).
    try:
        from reyn.chat.outbox import OutboxMessage
        await session._put_outbox(OutboxMessage(
            kind="system",
            text=f"plan discarded · {plan_id}",
            meta={"plan_id": plan_id, "source": "plan_aborted"},
        ))
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "plan_aborted outbox emit on /plan discard failed for %s: %r",
            plan_id, exc,
        )

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
        await session.router_host.delete_plan_decomposition(plan_id=plan_id)
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


def _parse_resume_args(args: str) -> tuple[str, str] | None:
    """Parse ``<plan_id> --from <step_id>`` into ``(plan_id, step_id)``.

    Returns None on parse failure so the caller can surface a usage
    error. Tolerant of extra whitespace; rejects missing flag / value.
    """
    parts = args.strip().split()
    if len(parts) < 3:
        return None
    plan_id = parts[0]
    if "--from" not in parts[1:]:
        return None
    flag_idx = parts.index("--from")
    if flag_idx + 1 >= len(parts):
        return None
    step_id = parts[flag_idx + 1]
    if not plan_id or not step_id:
        return None
    return (plan_id, step_id)


async def _resume_from_step(session: "ChatSession", args: str) -> None:
    """Reset step results from <step_id> onward and re-launch the plan.

    ADR-0023 §3.7 surgical operator escape hatch. Use cases:
      - A step recorded a result but the operator wants to redo it
        (= LLM produced something wrong, or world state shifted).
      - Step N failed transiently; operator wants to retry from step N
        without re-running steps 1..N-1.

    Steps:
      1. Validate args + plan existence.
      2. Cancel any currently-running task for this plan.
      3. Load the decomposition artifact (= for topological step order).
      4. Call PlanRegistry.reset_from_step (mutates + persists snapshot).
      5. Re-launch via ChatSession._spawn_resumed_plan with a fresh
         resume_plan derived from the post-reset snapshot.

    On any failure surface a /plan list-style hint so the operator
    knows whether to retry vs discard.
    """
    parsed = _parse_resume_args(args)
    if parsed is None:
        await reply_error(
            session,
            "Usage: /plan resume <plan_id> --from <step_id>",
        )
        return
    plan_id, step_id = parsed

    # Cancel any in-flight task for this plan first.
    task = session.running_plans.get(plan_id)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        session.running_plans.pop(plan_id, None)

    # Build per-agent PlanRegistry against the on-disk plan snapshots.
    from pathlib import Path

    from reyn.plan import (
        PlanRegistry,
        PlanResumeAnalyzer,
        PlanResumeCoordinator,
        PlanResumeDecision,
        read_decomposition,
    )

    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    plan_registry = PlanRegistry(
        agent_name=session.agent_name, agent_state_dir=agent_state_dir,
    )
    plan_registry.load_active()
    if plan_registry.get(plan_id) is None:
        await reply_error(session, f"unknown plan run: {plan_id}")
        return

    # Load the decomposition artifact for topological step order.
    try:
        decomposition = read_decomposition(agent_state_dir, plan_id)
    except FileNotFoundError:
        await reply_error(
            session,
            f"plan {plan_id}: decomposition artifact missing — "
            "use /plan discard to clean up",
        )
        return
    except Exception as exc:  # noqa: BLE001
        await reply_error(
            session,
            f"plan {plan_id}: cannot load decomposition ({exc!r})",
        )
        return

    step_order = [s.id for s in decomposition.steps]
    if step_id not in step_order:
        # Format known steps as a vertical bulleted list rather than a
        # Python ``repr`` of the list — the prior ``known: ['step1',
        # 'step2', 'step3']`` was a single very long quoted-string line
        # that broke into the ErrorBox header truncation and forced the
        # user to expand to read. A bulleted list reads naturally in
        # the expanded view, scales to longer step IDs, and surfaces
        # in the picker's hint-mode step completer as a discoverability
        # path so this error is rarer to begin with.
        steps_block = "\n".join(f"    - {s}" for s in step_order)
        await reply_error(
            session,
            f"plan {plan_id}: step {step_id!r} not in plan\n"
            f"  known steps:\n{steps_block}",
        )
        return

    # Mutate snapshot — clears results from step_id onward.
    ok = plan_registry.reset_from_step(
        plan_id=plan_id, from_step_id=step_id, step_order=step_order,
    )
    if not ok:
        await reply_error(
            session, f"plan {plan_id}: reset_from_step failed (see logs)",
        )
        return

    # Build a resume_plan from the now-mutated snapshot via the
    # analyzer (= empty WAL events; the snapshot's step_results is
    # the source of truth post-reset).
    analyzer = PlanResumeAnalyzer()
    snap = plan_registry.get(plan_id)
    # Synthetic events list: walk the snapshot's preserved
    # step_results and present them as completed pairs to the analyzer
    # so they classify as completed_with_result. Cleared steps have
    # no events → pending.
    synthetic_events: list[dict] = []
    for i, sid in enumerate(step_order):
        if sid in snap.step_results:
            synthetic_events.append({
                "seq": i * 2 + 1, "kind": "plan_step_started",
                "plan_id": plan_id, "step_id": sid,
            })
            synthetic_events.append({
                "seq": i * 2 + 2, "kind": "plan_step_completed",
                "plan_id": plan_id, "step_id": sid,
                "content_len": len(snap.step_results[sid]),
            })

    resume_plan = analyzer.analyze(
        snapshot=snap, decomposition=decomposition,
        wal_events=synthetic_events,
        agent_state_dir=agent_state_dir,
    )

    # Re-launch via the PlanRunner.spawn_resumed_plan path.
    decision = PlanResumeDecision(
        plan=resume_plan,
        action="retry_pending",
        pending_step_ids=resume_plan.pending_step_ids,
        child_actions={},
    )
    try:
        await session._plan_runner.spawn_resumed_plan(decision=decision)
    except Exception as exc:  # noqa: BLE001
        await reply_error(
            session,
            f"plan {plan_id}: respawn failed ({exc!r})",
        )
        return

    await reply(
        session,
        f"plan {plan_id}: resumed from step {step_id!r} "
        f"({len(resume_plan.pending_step_ids)} step(s) to re-execute)",
    )
