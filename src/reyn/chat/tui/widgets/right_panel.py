"""RightPanel — swappable right-side panel slot for the Reyn TUI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult, RenderResult
from textual.widget import Widget
from textual.widgets import Static, Tab, Tabs

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry

PANEL_TYPES: list[str] = ["keys", "events", "agents", "memory", "docs"]

_PANEL_LABELS: dict[str, str] = {
    "keys":    "Keys",
    "events":  "Events",
    "agents":  "Agents",
    "memory":  "Memory",
    "docs":    "Docs",
}

_TYPE_COLORS: dict[str, str] = {
    "user":      "#88aaff",
    "feedback":  "#ffaa44",
    "project":   "#44cc88",
    "reference": "#cc88ff",
}

_LIVE_PANELS = {"events", "agents"}
_REFRESH_INTERVAL = 2.0


def _esc(s: str) -> str:
    """Escape Rich markup brackets in plain strings."""
    return s.replace("[", "\\[").replace("]", "\\]")


class _PanelContent(Static):
    """Static subclass that delegates render() to the parent RightPanel.

    By overriding render() instead of calling update(), we avoid any
    intermediate storage that could end up in Textual's visual pipeline
    with the wrong type.
    """

    def __init__(self, panel: "RightPanel", **kwargs) -> None:
        super().__init__("", **kwargs)
        self._panel = panel

    def render(self) -> RenderResult:
        try:
            return self._panel._panel_markup()
        except Exception:
            return ""

    def invalidate(self) -> None:
        """Force a re-render on the next frame."""
        self._layout_cache.clear()
        self.refresh(layout=True)


class RightPanel(Widget):
    """Swappable right-side panel with tab bar.

    Toggled open/close by ctrl+b.
    Tab cycling: ctrl+o (next) / ctrl+shift+o (prev, terminal-dependent).
    """

    DEFAULT_CSS = """
    RightPanel {
        display: none;
        width: 33%;
        min-width: 30;
        max-width: 60;
        border-left: tall #2a2a2a;
        background: #111111;
        layout: vertical;
        height: 100%;
    }

    RightPanel Tabs {
        background: #1a1a1a;
        height: 2;
    }

    RightPanel Tab {
        color: #666666;
        padding: 0 2;
    }

    RightPanel Tab.-active {
        color: #C8553D;
        text-style: bold;
    }

    RightPanel Tab:hover {
        color: #aaaaaa;
    }

    RightPanel .underline--bar {
        color: #2a2a2a;
    }

    RightPanel #panel-content {
        height: 1fr;
        color: #666666;
        padding: 1 1;
    }
    """

    def __init__(
        self,
        *,
        registry: "AgentRegistry | None" = None,
        project_root: Path | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._registry = registry
        self._project_root = project_root
        self._panel_type = PANEL_TYPES[0]

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Tabs(
            *[Tab(_PANEL_LABELS[t], id=t) for t in PANEL_TYPES],
            id="panel-tabs",
        )
        yield _PanelContent(self, id="panel-content")

    def on_mount(self) -> None:
        self.set_interval(_REFRESH_INTERVAL, self._refresh_live)

    # ── public API ───────────────────────────────────────────────────────────

    @property
    def panel_type(self) -> str:
        return self._panel_type

    def cycle(self, delta: int) -> None:
        """Advance (delta=+1) or retreat (delta=-1) through tabs."""
        tabs = self.query_one("#panel-tabs", Tabs)
        if delta > 0:
            tabs.action_next_tab()
        else:
            tabs.action_previous_tab()

    # ── tab activation ───────────────────────────────────────────────────────

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab and event.tab.id in PANEL_TYPES:
            self._panel_type = event.tab.id
            self._invalidate()

    # ── content refresh ──────────────────────────────────────────────────────

    def _refresh_live(self) -> None:
        if self._panel_type in _LIVE_PANELS:
            self._invalidate()

    def _invalidate(self) -> None:
        try:
            self.query_one("#panel-content", _PanelContent).invalidate()
        except Exception:
            pass

    # ── render dispatch ───────────────────────────────────────────────────────

    def _panel_markup(self) -> str:
        try:
            if self._panel_type == "keys":
                return self._render_keys()
            if self._panel_type == "events":
                return self._render_events()
            if self._panel_type == "agents":
                return self._render_agents()
            if self._panel_type == "memory":
                return self._render_memory()
            if self._panel_type == "docs":
                return self._render_docs()
        except Exception as e:
            return f"[red]error: {_esc(str(e))}[/red]"
        return ""

    # ── panel renderers ──────────────────────────────────────────────────────

    def _render_keys(self) -> str:
        lines = ["[bold #C8553D]Key Bindings[/]\n"]
        bindings = self.app.active_bindings
        seen: set[str] = set()
        for _key, ab in bindings.items():
            b = ab.binding
            if b.key in seen or not b.description:
                continue
            seen.add(b.key)
            key_col = f"{_esc(b.key):<18}"
            desc_col = _esc(b.description)
            lines.append(f"[#aaaaaa]  {key_col}[/]  [#dddddd]{desc_col}[/]")
        if len(lines) == 1:
            lines.append("[#555555]  (no bindings)[/]")
        return "\n".join(lines)

    def _render_events(self) -> str:
        lines = ["[bold #C8553D]Recent Events[/]\n"]

        if self._project_root is None:
            lines.append("[#555555]  (no project root)[/]")
            return "\n".join(lines)

        events_root = self._project_root / ".reyn" / "events"
        if not events_root.is_dir():
            lines.append("[#555555]  (no events yet)[/]")
            return "\n".join(lines)

        all_events: list[dict] = []
        for jsonl in sorted(events_root.rglob("*.jsonl")):
            try:
                for line in jsonl.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            all_events.append(json.loads(line))
                        except Exception:
                            pass
            except Exception:
                pass

        if not all_events:
            lines.append("[#555555]  (no events yet)[/]")
            return "\n".join(lines)

        for ev in all_events[-30:][::-1]:
            ts = _esc(str(ev.get("timestamp", ""))[:19].replace("T", " "))
            ev_type = _esc(str(ev.get("type", "?")))
            lines.append(f"[#555555]  {ts}[/]  [#aaaaaa]{ev_type}[/]")

        return "\n".join(lines)

    def _render_agents(self) -> str:
        lines = ["[bold #C8553D]Agents[/]\n"]

        if self._registry is None:
            lines.append("[#555555]  (no registry)[/]")
            return "\n".join(lines)

        names = self._registry.list_names()
        if not names:
            lines.append("[#555555]  (no agents)[/]")
            return "\n".join(lines)

        attached = self._registry.attached_name
        loaded = set(self._registry.loaded_names())

        for name in names:
            is_attached = name == attached
            prefix = "▶ " if is_attached else "  "
            name_style = "bold #C8553D" if is_attached else "#dddddd"
            status = "running" if name in loaded else "idle"
            status_style = "#44cc88" if name in loaded else "#555555"
            name_col = f"{_esc(name):<20}"
            lines.append(
                f"[#555555]  {prefix}[/]"
                f"[{name_style}]{name_col}[/]"
                f"  [{status_style}]{status}[/]"
            )
            try:
                last = self._registry.last_activity_at(name)
                if last:
                    ts = _esc(last.strftime("%Y-%m-%d %H:%M"))
                    lines.append(f"[#555555]    last: {ts}[/]")
            except Exception:
                pass

        return "\n".join(lines)

    def _render_memory(self) -> str:
        lines = ["[bold #C8553D]Memory[/]\n"]

        if self._project_root is None:
            lines.append("[#555555]  (no project root)[/]")
            return "\n".join(lines)

        from reyn.memory.memory import list_entries
        entries = list_entries(self._project_root / ".reyn" / "memory")

        if not entries:
            lines.append("[#555555]  (no memories)[/]")
            return "\n".join(lines)

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
            lines.append(f"[bold {color}]  [{type_key.upper()}][/]")
            for e in group:
                lines.append(f"[#dddddd]    {_esc(e.name)}[/]")
                if e.description:
                    lines.append(f"[#555555]      {_esc(e.description)}[/]")
            lines.append("")

        if other:
            lines.append("[bold #888888]  [OTHER][/]")
            for e in other:
                lines.append(f"[#dddddd]    {_esc(e.name)}[/]")

        return "\n".join(lines)

    def _render_docs(self) -> str:
        lines = ["[bold #C8553D]Docs[/]\n"]

        if self._project_root is None:
            lines.append("[#555555]  (no project root)[/]")
            return "\n".join(lines)

        docs_root = self._project_root / "docs" / "en"
        if not docs_root.is_dir():
            lines.append("[#555555]  (docs/en/ not found)[/]")
            return "\n".join(lines)

        groups: dict[str, list[Path]] = {}
        for md in sorted(docs_root.rglob("*.md")):
            rel = md.relative_to(docs_root)
            section = rel.parts[0] if len(rel.parts) > 1 else ""
            groups.setdefault(section, []).append(md)

        for section in sorted(groups):
            label = section.upper() if section else "ROOT"
            lines.append(f"[bold #aaaaaa]  [{_esc(label)}][/]")
            for md in groups[section]:
                rel = md.relative_to(docs_root)
                depth = len(rel.parts) - 1
                indent = "    " + "  " * max(0, depth - 1)
                lines.append(f"[#666666]{indent}{_esc(md.stem)}[/]")
            lines.append("")

        return "\n".join(lines)
