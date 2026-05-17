"""Memory tab — renders shared and per-agent memory entries with cursor."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import _CORAL, _esc

_TYPE_COLORS: dict[str, str] = {
    "user":      "#88aaff",
    "feedback":  "#ffaa44",
    "project":   "#44cc88",
    "reference": "#cc88ff",
}


def render_memory(
    project_root: Path | None,
    *,
    cursor: int = 0,
) -> tuple[str, list[Any]]:
    """Return Rich markup + the flat ordered list of MemoryEntry items.

    The flat list lets the orchestrator drive cursor navigation and the
    Enter→preview integration without re-walking the disk. The row at
    index ``cursor`` is highlighted with a coral ▶ prefix.
    """
    if project_root is None:
        return "[#555555]  (no project root)[/]", []

    from reyn.memory.memory import list_entries

    lines: list[str] = []
    flat_entries: list[Any] = []

    def _render_scope(entries: list, label: str, label_color: str) -> None:
        lines.append(f"[bold {label_color}]  {_esc(label)}[/]")
        if not entries:
            lines.append(
                "[#555555]    (empty — ask reyn to \"remember <fact>\")[/]"
            )
            lines.append("")
            return
        groups: dict[str, list] = {
            t: [] for t in ("user", "feedback", "project", "reference")
        }
        other: list = []
        for e in entries:
            if e.type in groups:
                groups[e.type].append(e)
            else:
                other.append(e)
        for type_key in ("user", "feedback", "project", "reference"):
            group = groups[type_key]
            if not group:
                continue
            color = _TYPE_COLORS[type_key]
            lines.append(f"[bold {color}]    \\[{type_key.upper()}][/]")
            for e in group:
                flat_entries.append(e)
                is_cursor = (len(flat_entries) - 1) == cursor
                indent = f"[bold {_CORAL}]    ▶ [/]" if is_cursor else "      "
                name_style = f"bold {_CORAL}" if is_cursor else "#dddddd"
                lines.append(f"{indent}[{name_style}]{_esc(e.name)}[/]")
                if e.description:
                    lines.append(f"[#555555]        {_esc(e.description)}[/]")
        if other:
            lines.append("[bold #888888]    \\[OTHER][/]")
            for e in other:
                flat_entries.append(e)
                is_cursor = (len(flat_entries) - 1) == cursor
                indent = f"[bold {_CORAL}]    ▶ [/]" if is_cursor else "      "
                name_style = f"bold {_CORAL}" if is_cursor else "#dddddd"
                lines.append(f"{indent}[{name_style}]{_esc(e.name)}[/]")
        lines.append("")

    # Shared memory
    shared = list_entries(project_root / ".reyn" / "memory")
    _render_scope(shared, "SHARED", _CORAL)

    # Per-agent memory
    agents_dir = project_root / ".reyn" / "agents"
    if agents_dir.exists():
        for agent_dir in sorted(agents_dir.iterdir()):
            mem_dir = agent_dir / "memory"
            if not mem_dir.exists():
                continue
            agent_entries = list_entries(mem_dir)
            _render_scope(agent_entries, f"AGENT  {agent_dir.name}", "#7a9fc7")

    return "\n".join(lines), flat_entries


__all__ = ["render_memory"]
