"""Memory tab — renders shared and per-agent memory entries with cursor."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .base import _CORAL, _esc

_TYPE_COLORS: dict[str, str] = {
    "user":      "#88aaff",
    "feedback":  "#ffaa44",
    "project":   "#44cc88",
    "reference": "#cc88ff",
}


_HOT_LIST_MAX_VISIBLE = 8


def _fmt_ago(last_ts: Any) -> str:
    """Best-effort relative-time string for the hot-list ``last_ts`` field.

    The ARS forwarder emits ``last_ts`` as a Unix-epoch float (see
    ``ActionUsageTracker.full_ranking``). Returns an empty string when
    the value is missing, unparseable, or in the future — callers append
    a dim suffix only when this returns non-empty so the layout stays
    intact for older payloads that omitted the field.
    """
    try:
        ts = float(last_ts)
    except (TypeError, ValueError):
        return ""
    delta = time.time() - ts
    if delta < 0 or ts <= 0:
        return ""
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def render_memory(
    project_root: Path | None,
    *,
    cursor: int = 0,
    hot_list: list[dict] | None = None,
    type_filter: str | None = None,
) -> tuple[str, list[Any], list[int]]:
    """Return Rich markup + the flat ordered list of MemoryEntry items
    + the y-coordinate (= 0-indexed line number) of each entry's name row.

    The flat list lets the orchestrator drive cursor navigation and the
    Enter→preview integration without re-walking the disk. The row at
    index ``cursor`` is highlighted with a coral ▶ prefix.

    ``entry_ys[i]`` is the line-index of ``flat_entries[i]``'s name row in
    the rendered output. Section labels, per-type subheaders and blank
    separators all bump the y, so the orchestrator can't predict it
    arithmetically — we record it here, where the structure is known.

    ``hot_list`` (issue #192): the latest ARS qualified-name ranking
    from ``ChatLifecycleForwarder.on_hot_list_updated``. When non-empty,
    a "Hot now" sub-section renders above SHARED / AGENT scopes so the
    user can see why the router preferred skill X over Y on the last
    turn. The hot list is **not** part of ``flat_entries`` — entries
    listed there are MemoryEntry items, and the hot list carries
    qualified action names which are a different kind of object.
    """
    if project_root is None:
        return "[#555555]  (no project root)[/]", [], []

    from reyn.memory.memory import list_entries

    lines: list[str] = []
    flat_entries: list[Any] = []
    entry_ys: list[int] = []
    # Wave-11 A#1 — memory-type filter banner. When type_filter is one
    # of the four known kinds, prepend a 2-row banner so the user has
    # a persistent cue that the list is narrowed (rather than the
    # missing types looking like "this scope has no FEEDBACK entries
    # actually"). Press [t] to cycle next; banner disappears when
    # back to None.
    if type_filter in _TYPE_COLORS:
        banner_color = _TYPE_COLORS[type_filter]
        lines.append(
            f"  [bold {banner_color}]⌕ filter: [/]"
            f"[bold {_CORAL}]{_esc(type_filter.upper())}[/]"
        )
        lines.append(
            "  [#555555]  press [/][bold #aaaaaa]\\[t][/]"
            "[#555555] to cycle filter[/]"
        )

    # Hot now section (issue #192). Always renders the header so the
    # feature is discoverable on cold-start (= before any router
    # activity). Wave-4 PC5: previously the entire section was
    # conditionally hidden when ``hot_list`` was empty, so first-launch
    # users never learned the section existed. Now the header is
    # always there + a dim placeholder line when no data, populated
    # rows once the ARS forwarder emits ``hot_list_updated``.
    # Capped at _HOT_LIST_MAX_VISIBLE so a long ranking doesn't push
    # the SHARED / AGENT entries off the top of a narrow panel.
    lines.append("[bold #ffaa44]  HOT NOW[/]")
    if hot_list:
        for entry in hot_list[:_HOT_LIST_MAX_VISIBLE]:
            try:
                name = str(entry.get("qualified_name", ""))
                freq = int(entry.get("freq", 0))
            except (AttributeError, ValueError, TypeError):
                continue
            if not name:
                continue
            if freq <= 0:
                continue
            ago = _fmt_ago(entry.get("last_ts") if hasattr(entry, "get") else None)
            ago_suffix = f"  [#555555]{ago}[/]" if ago else ""
            lines.append(
                f"[#ffaa44]    🔥 [/][#dddddd]{_esc(name)}[/]  "
                f"[#666666]×{freq}[/]{ago_suffix}"
            )
        overflow = len(hot_list) - _HOT_LIST_MAX_VISIBLE
        if overflow > 0:
            lines.append(
                f"[#555555]    … {overflow} more[/]"
            )
    else:
        lines.append("[#555555]    (no router activity yet)[/]")
    lines.append("")

    def _render_scope(entries: list, label: str, label_color: str) -> None:
        lines.append(f"[bold {label_color}]  {_esc(label)}[/]")
        if not entries:
            # Two short lines instead of one long one — the previous
            # single line ``(empty — ask reyn to "remember <fact>")``
            # (45 cells incl. indent) clipped to ``(empty — ask reyn to
            # "re…`` at the default 33%-panel content width (~22 cells).
            # Splitting preserves both the "empty" signal and the
            # call-to-action and survives narrow panes.
            lines.append("[#555555]    (empty)[/]")
            lines.append(
                "[#555555]    try: \"remember <fact>\"[/]"
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
        # Wave-11 A#1 — when type_filter is active, drop every type
        # except the one named. ``other`` is also wiped because the
        # filter contract is "show ONLY this type". Sub-headers for
        # skipped types vanish too because the ``if not group``
        # guard below skips empty buckets.
        if type_filter in groups:
            for k in list(groups.keys()):
                if k != type_filter:
                    groups[k] = []
            other = []
        for type_key in ("user", "feedback", "project", "reference"):
            group = groups[type_key]
            if not group:
                continue
            color = _TYPE_COLORS[type_key]
            lines.append(f"[bold {color}]    \\[{type_key.upper()}][/]")
            for e in group:
                flat_entries.append(e)
                entry_ys.append(len(lines))
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
                entry_ys.append(len(lines))
                is_cursor = (len(flat_entries) - 1) == cursor
                indent = f"[bold {_CORAL}]    ▶ [/]" if is_cursor else "      "
                name_style = f"bold {_CORAL}" if is_cursor else "#dddddd"
                lines.append(f"{indent}[{name_style}]{_esc(e.name)}[/]")
                if e.description:
                    lines.append(f"[#555555]        {_esc(e.description)}[/]")
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

    return "\n".join(lines), flat_entries, entry_ys


__all__ = ["render_memory"]
