"""Memory tab — renders shared and per-agent memory entries."""
from __future__ import annotations

from pathlib import Path

from .base import _CORAL, _esc

_TYPE_COLORS: dict[str, str] = {
    "user":      "#88aaff",
    "feedback":  "#ffaa44",
    "project":   "#44cc88",
    "reference": "#cc88ff",
}


def render_memory(project_root: Path | None) -> str:
    """Return Rich markup listing all known memory entries."""
    if project_root is None:
        return "[#555555]  (no project root)[/]"

    from reyn.memory.memory import list_entries

    lines: list[str] = []

    def _render_scope(entries: list, label: str, label_color: str) -> None:
        lines.append(f"[bold {label_color}]  {_esc(label)}[/]")
        if not entries:
            lines.append("[#555555]    (empty)[/]")
            lines.append("")
            return
        groups: dict[str, list] = {t: [] for t in ("user", "feedback", "project", "reference")}
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
                lines.append(f"[#dddddd]      {_esc(e.name)}[/]")
                if e.description:
                    lines.append(f"[#555555]        {_esc(e.description)}[/]")
        if other:
            lines.append("[bold #888888]    \\[OTHER][/]")
            for e in other:
                lines.append(f"[#dddddd]      {_esc(e.name)}[/]")
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

    return "\n".join(lines)


__all__ = ["render_memory"]
