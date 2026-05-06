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
        else:
            # idle: last activity
            try:
                last = registry.last_activity_at(name)
                if last:
                    ts = last.strftime("%Y-%m-%d %H:%M")
                    tree.add(RichText(f"last: {ts}", style="#555555"))
            except Exception as exc:
                logger.warning(
                    "right_panel agents: registry.last_activity_at(%s) failed: %s",
                    name, exc,
                )

        agent_trees.append(tree)

    # interleave blank lines between agent blocks
    items: list[Any] = []
    for i, tree in enumerate(agent_trees):
        if i > 0:
            items.append(RichText(""))
        items.append(tree)
    return RichGroup(*items)


__all__ = ["render_agents"]
