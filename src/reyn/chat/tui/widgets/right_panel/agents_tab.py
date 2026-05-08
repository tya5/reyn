"""Agents tab — Rich Tree view of registered agents and their running skills."""
from __future__ import annotations

import time as _time
from typing import TYPE_CHECKING, Any

from rich.console import Group as RichGroup
from rich.text import Text as RichText
from rich.tree import Tree as RichTree

from .base import _CORAL, logger

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry


def _plans_for_agent(registry: "AgentRegistry", name: str) -> list[dict]:
    """Inspect the loaded session and return a list of plan-summary dicts.

    Each entry has: ``plan_id`` (8-char prefix), ``goal`` (≤48 chars),
    ``done`` (completed step count), ``failed`` (failed step count),
    ``total`` (total step count), ``status`` (running / paused).

    Defensive — main hasn't been rebased into this branch yet, so
    ``running_plans`` / ``_get_plan_registry`` may not exist on the session.
    Returns ``[]`` for any failure path so the agents tab keeps rendering.
    """
    out: list[dict] = []
    try:
        session = registry._agents.get(name)  # type: ignore[attr-defined]
    except Exception:
        return out
    if session is None:
        return out

    running = getattr(session, "running_plans", None) or {}
    plan_reg = None
    getter = getattr(session, "_get_plan_registry", None)
    if callable(getter):
        try:
            plan_reg = getter()
        except Exception as exc:
            logger.warning(
                "right_panel agents: _get_plan_registry(%s) failed: %s",
                name, exc,
            )
            plan_reg = None

    # plan_ids = union of in-flight tasks + every persisted snapshot. The
    # snapshot side covers paused / interrupted plans that have no live
    # task but still have recovery state on disk.
    seen: set[str] = set()
    plan_ids: list[str] = []
    for pid in running.keys():
        if pid not in seen:
            plan_ids.append(pid)
            seen.add(pid)
    if plan_reg is not None:
        try:
            for pid in plan_reg.list_active():
                if pid not in seen:
                    plan_ids.append(pid)
                    seen.add(pid)
        except Exception as exc:
            logger.warning(
                "right_panel agents: plan_registry.list_active(%s) failed: %s",
                name, exc,
            )

    for plan_id in plan_ids:
        snap = None
        if plan_reg is not None:
            try:
                snap = plan_reg.get(plan_id)
            except Exception:
                snap = None
        goal = getattr(snap, "goal", "") if snap is not None else ""
        step_results = getattr(snap, "step_results", {}) if snap is not None else {}
        step_failures = getattr(snap, "step_failures", {}) if snap is not None else {}
        steps_serialized = (
            getattr(snap, "steps_serialized", []) if snap is not None else []
        )
        total = len(steps_serialized) if steps_serialized else (
            len(step_results) + len(step_failures)
        )
        task = running.get(plan_id)
        is_running = task is not None and not task.done()
        out.append({
            "plan_id": plan_id[:8],
            "goal": goal,
            "done": len(step_results),
            "failed": len(step_failures),
            "total": total,
            "status": "running" if is_running else "paused",
        })
    return out


def render_agents(
    registry: "AgentRegistry | None",
    exec_state: dict[str, dict],
) -> Any:
    """Return a Rich renderable describing each agent and its running skills."""
    if registry is None:
        return "[#555555]  (no registry)[/]"

    try:
        names = registry.list_names()
    except Exception as exc:
        logger.warning("right_panel agents: registry.list_names() failed: %s", exc)
        return "[#555555]  (registry unavailable)[/]"

    if not names:
        return "[#555555]  (no agents)[/]"

    try:
        attached = registry.attached_name
    except Exception as exc:
        logger.warning("right_panel agents: registry.attached_name unavailable: %s", exc)
        attached = None
    try:
        loaded = set(registry.loaded_names())
    except Exception as exc:
        logger.warning("right_panel agents: registry.loaded_names() failed: %s", exc)
        loaded = set()
    now = _time.monotonic()

    agent_trees: list[Any] = []

    for name in names:
        is_attached = name == attached
        in_loaded = name in loaded

        # ── agent label ────────────────────────────────────────────
        label = RichText()
        label.append("▶ " if is_attached else "  ", style="#555555")
        label.append(name, style="bold " + _CORAL if is_attached else "#dddddd")
        label.append("  ")
        label.append(
            "● running" if in_loaded else "○ idle",
            style="#44cc88" if in_loaded else "#555555",
        )

        tree = RichTree(label, guide_style="#333333")

        # ── running skills ─────────────────────────────────────────
        agent_skills = [
            (rid, info)
            for rid, info in exec_state.items()
            if info.get("agent_name") == name
        ]

        agent_plans = _plans_for_agent(registry, name)

        if agent_skills:
            for run_id, info in agent_skills:
                elapsed = int(now - info.get("start_time", now))
                skill_label = RichText()
                skill_label.append(f"[{elapsed:3d}s] ", style="#888888")
                skill_label.append(
                    info.get("skill_name", "?"), style="#dddddd"
                )
                skill_node = tree.add(skill_label)

                phase = info.get("phase", "")
                if phase:
                    visits = info.get("phase_visits", 1)
                    phase_label = RichText()
                    phase_label.append(phase, style="#555555")
                    if visits > 1:
                        phase_label.append(f"  v{visits}", style="#444444")
                    skill_node.add(phase_label)

        # Plan-mode (ADR-0022 / 0023). Surfaced as a sibling of running
        # skills — same agent can simultaneously run skills + plans.
        # Coloured orange (#ff9944) to match the events-tab plan_* family.
        if agent_plans:
            for p in agent_plans:
                plan_label = RichText()
                plan_label.append("plan ", style="#888888")
                plan_label.append(p["plan_id"], style="#ff9944")
                plan_label.append(
                    f"  {p['done']}/{p['total']}",
                    style="#dddddd",
                )
                if p["failed"]:
                    plan_label.append(
                        f"  ({p['failed']} failed)", style="#ff6644",
                    )
                plan_label.append(
                    f"  {p['status']}",
                    style="#44cc88" if p["status"] == "running" else "#aaaa55",
                )
                plan_node = tree.add(plan_label)
                if p["goal"]:
                    goal = p["goal"][:60] + ("…" if len(p["goal"]) > 60 else "")
                    plan_node.add(RichText(goal, style="#555555"))

        if not agent_skills and not agent_plans:
            # idle: last activity + message count + recent user snippet
            try:
                last = registry.last_activity_at(name)
                ts_str = last.strftime("%Y-%m-%d %H:%M") if last else None
            except Exception as exc:
                logger.warning(
                    "right_panel agents: registry.last_activity_at(%s) failed: %s",
                    name, exc,
                )
                ts_str = None
            try:
                msg_count = registry.message_count(name)
            except Exception:
                msg_count = 0
            try:
                snippet = registry.recent_user_message(name)
            except Exception:
                snippet = ""
            if ts_str:
                count_part = (
                    f"  ·  {msg_count} message{'s' if msg_count != 1 else ''}"
                    if msg_count > 0 else ""
                )
                tree.add(RichText(
                    f"last: {ts_str}{count_part}", style="#555555",
                ))
                if snippet:
                    _max = 60
                    short = (
                        snippet if len(snippet) <= _max
                        else snippet[:_max - 1] + "…"
                    )
                    line2 = RichText()
                    line2.append("↳ ", style="#555555")
                    line2.append(short, style="#444444")
                    tree.add(line2)

        agent_trees.append(tree)

    # interleave blank lines between agent blocks
    items: list[Any] = []
    for i, tree in enumerate(agent_trees):
        if i > 0:
            items.append(RichText(""))
        items.append(tree)
    return RichGroup(*items)


__all__ = ["render_agents"]
