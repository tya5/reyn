"""RightPanel — swappable right-side panel slot for the Reyn TUI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult, RenderResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Label, RichLog, Static, Tab, Tabs

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry

PANEL_TYPES: list[str] = ["keys", "events", "agents", "memory", "cost", "docs"]

_PANEL_LABELS: dict[str, str] = {
    "keys":    "Keys",
    "events":  "Events",
    "agents":  "Agents",
    "memory":  "Memory",
    "cost":    "Cost",
    "docs":    "Docs",
}

_TYPE_COLORS: dict[str, str] = {
    "user":      "#88aaff",
    "feedback":  "#ffaa44",
    "project":   "#44cc88",
    "reference": "#cc88ff",
}

_LIVE_PANELS = {"events", "agents", "cost"}
_REFRESH_INTERVAL = 2.0

# ── events tab constants ──────────────────────────────────────────────────────

_EVENT_COLORS: dict[str, str] = {
    "phase_started":               "#44cc88",
    "phase_completed":             "#44cc88",
    "control_decided":             "#88ddaa",
    "context_built":               "#335544",
    "llm_called":                  "#ffcc66",
    "llm_response_received":       "#ffcc66",
    "artifact_created":            "#88aaff",
    "artifact_validated":          "#88aaff",
    "validation_error":            "#ff6644",
    "phase_retry":                 "#ff6644",
    "permission_denied":           "#ff4444",
    "router_retry_exhausted":      "#ff4444",
    "tool_failed":                 "#ff6644",
    "tool_called":                 "#cc88ff",
    "tool_returned":               "#cc88ff",
    "mcp_called":                  "#cc88ff",
    "mcp_completed":               "#cc88ff",
    "act_executed":                "#cc88ff",
    "skill_run_spawned":           "#88aaff",
    "skill_run_completed":         "#88aaff",
    "workflow_started":            "#88aaff",
    "workflow_finished":           "#88aaff",
    "agent_message_sent":          "#aaaaaa",
    "agent_request_received":      "#aaaaaa",
    "agent_response_received":     "#aaaaaa",
    "user_message_received":       "#dddddd",
    "chat_started":                "#dddddd",
    "chat_stopped":                "#dddddd",
    "user_intervention_requested": "#ffcc88",
    "user_intervention_received":  "#ffcc88",
    "preprocessor_step_started":   "#555555",
    "preprocessor_step_completed": "#555555",
    "python_step_started":         "#555555",
    "python_step_completed":       "#555555",
    "web_fetch_started":           "#888888",
    "web_fetch_completed":         "#888888",
    "web_search_started":          "#888888",
    "web_search_completed":        "#888888",
    "workspace_updated":           "#555555",
    "compaction_check":            "#555555",
}
_DEFAULT_EVENT_COLOR = "#666666"

# Each tuple is (label, frozenset-of-types). Empty set = show all.
_FILTER_GROUPS: list[tuple[str, frozenset]] = [
    ("all",   frozenset()),
    ("phase", frozenset({
        "phase_started", "phase_completed", "control_decided", "context_built",
        "artifact_created", "artifact_validated",
    })),
    ("llm",   frozenset({"llm_called", "llm_response_received"})),
    ("tool",  frozenset({
        "tool_called", "tool_returned", "tool_failed",
        "mcp_called", "mcp_completed", "act_executed",
    })),
    ("skill", frozenset({
        "skill_run_spawned", "skill_run_completed",
        "workflow_started", "workflow_finished",
        "agent_message_sent", "agent_request_received", "agent_response_received",
    })),
    ("error", frozenset({
        "validation_error", "phase_retry", "permission_denied",
        "router_retry_exhausted", "tool_failed",
    })),
    ("user", frozenset({
        "user_message_received",
        "user_intervention_requested", "user_intervention_received",
        "chat_started", "chat_stopped",
    })),
]

_TAIL_CYCLE: list[int] = [30, 50, 100, 200]


def _event_hint(ev: dict) -> str:
    """Return a short plain-text annotation of the most useful data fields."""
    t = ev.get("type", "")
    d = ev.get("data") or {}

    if t == "phase_started":
        return d.get("phase", "")
    if t == "phase_completed":
        nxt = d.get("next") or "finish"
        conf = d.get("confidence", 0)
        return f"{d.get('phase', '')} → {nxt} ({conf:.0%})"
    if t == "control_decided":
        nxt = d.get("next_phase") or ""
        suffix = f" → {nxt}" if nxt else ""
        return f"{d.get('phase', '')}: {d.get('decision', '')}{suffix}"
    if t == "llm_called":
        return f"{d.get('phase', '')} [{d.get('model', '')}]"
    if t == "llm_response_received":
        pt = d.get("prompt_tokens", 0)
        ct = d.get("completion_tokens", 0)
        cost = d.get("cost_usd", 0)
        return f"{pt}+{ct}t ${cost:.4f}"
    if t == "artifact_created":
        return f"{d.get('artifact_type', '')} @ {d.get('phase', '')}"
    if t == "artifact_validated":
        errors = d.get("errors") or []
        at = d.get("artifact_type", "")
        return f"{at} ✗ {len(errors)} err" if errors else f"{at} ✓"
    if t == "validation_error":
        return f"{d.get('phase', '')}: {str(d.get('error', ''))[:35]}"
    if t == "phase_retry":
        return f"attempt {d.get('attempt', '?')}/{d.get('max_retries', '?')}: {str(d.get('error', ''))[:25]}"
    if t == "permission_denied":
        return f"{d.get('kind', '')} {d.get('path', '')}"
    if t in ("tool_called", "tool_returned"):
        return d.get("tool", "")
    if t == "tool_failed":
        return f"{d.get('tool', '')}: {str(d.get('message', ''))[:25]}"
    if t in ("mcp_called", "mcp_completed"):
        suffix = " ✗" if d.get("is_error") else ""
        return f"{d.get('server', '')}.{d.get('tool', '')}{suffix}"
    if t == "workflow_started":
        run_id = str(d.get("run_id", ""))[:8]
        return f"{d.get('skill', '')} [{run_id}]"
    if t == "workflow_finished":
        conf = d.get("confidence", 0)
        return f"{d.get('skill', '')} ({conf:.0%})"
    if t == "skill_run_spawned":
        return d.get("skill", "")
    if t == "skill_run_completed":
        return f"{d.get('skill', '')} [{d.get('status', '')}]"
    if t == "agent_message_sent":
        return f"{d.get('from_agent', '')} → {d.get('to_agent', '')}"
    if t in ("agent_request_received", "agent_response_received"):
        return d.get("from_agent", "")
    if t == "user_message_received":
        text = str(d.get("text", ""))
        return text[:40] + ("…" if len(text) > 40 else "")
    if t == "user_intervention_requested":
        return str(d.get("question", ""))[:40]
    if t == "user_intervention_received":
        return str(d.get("answer", ""))[:40]
    if t == "web_fetch_started":
        return str(d.get("url", ""))[:45]
    if t == "web_fetch_completed":
        return f"HTTP {d.get('status_code', '')} {d.get('content_length', '')}b"
    if t == "web_search_started":
        return str(d.get("query", ""))[:40]
    if t == "web_search_completed":
        return f"{d.get('result_count', '')} results"
    return ""


def _load_chain_replies(project_root: Path) -> dict[str, str]:
    """Return {chain_id: last_agent_reply_text} from all agents' history files."""
    replies: dict[str, str] = {}
    agents_dir = project_root / ".reyn" / "agents"
    if not agents_dir.is_dir():
        return replies
    for hist in agents_dir.glob("*/history.jsonl"):
        try:
            for raw in hist.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    m = json.loads(raw)
                    if m.get("role") == "agent":
                        cid = (m.get("meta") or {}).get("chain_id")
                        if cid:
                            replies[cid] = m.get("text", "")
                except Exception:
                    pass
        except Exception:
            pass
    return replies


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


class _PreviewPane(Widget):
    """Lower-half preview pane toggled with 'f'.

    Generic: any tab can populate it. Currently docs tab shows the focused
    file's Markdown content. Other tabs may use it in future.
    """

    can_focus = True

    DEFAULT_CSS = """
    _PreviewPane {
        display: none;
        height: 1fr;
        border-top: tall #2a2a2a;
        layout: vertical;
    }
    _PreviewPane.preview-visible {
        display: block;
    }
    _PreviewPane #preview-header {
        height: 1;
        color: #555555;
        background: #1a1a1a;
        padding: 0 1;
    }
    _PreviewPane:focus {
        border-top: tall #C8553D;
    }
    _PreviewPane:focus #preview-header {
        color: #C8553D;
    }
    _PreviewPane RichLog {
        background: transparent;
        height: 1fr;
        padding: 0 1;
        overflow-x: auto;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Label("", id="preview-header")
        yield RichLog(id="preview-log", markup=False, highlight=False, auto_scroll=False)

    def on_key(self, event) -> None:
        if event.key == "up":
            event.prevent_default()
            self.scroll_line(-1)
        elif event.key == "down":
            event.prevent_default()
            self.scroll_line(+1)

    def show_markdown(self, path: Path) -> None:
        from rich.markdown import Markdown as RichMarkdown
        self._current_path = path
        try:
            log = self.query_one("#preview-log", RichLog)
            log.clear()
            log.write(RichMarkdown(path.read_text(encoding="utf-8")))
            log.scroll_home(animate=False)
            self._update_header()
        except Exception:
            pass

    def scroll_line(self, delta: int) -> None:
        try:
            log = self.query_one("#preview-log", RichLog)
            if delta > 0:
                log.scroll_down(1, animate=False)
            else:
                log.scroll_up(1, animate=False)
        except Exception:
            pass

    def clear(self) -> None:
        self._current_path = None
        try:
            self.query_one("#preview-log", RichLog).clear()
            self._update_header()
        except Exception:
            pass

    def _update_header(self) -> None:
        name = _esc(self._current_path.name) if self._current_path else "—"
        try:
            self.query_one("#preview-header", Label).update(
                f"  {name}  │  ↑/↓=scroll"
            )
        except Exception:
            pass


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

    RightPanel #panel-scroll {
        height: 1fr;
    }

    RightPanel #panel-content {
        height: auto;
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
        self._event_filter_idx: int = 0
        self._event_tail_idx: int = 0
        self._docs_cursor: int = 0
        self._docs_files: list[Path] = []
        self._docs_groups: dict[str, list[Path]] = {}
        self._preview_visible: bool = False

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Tabs(
            *[Tab(_PANEL_LABELS[t], id=t) for t in PANEL_TYPES],
            id="panel-tabs",
        )
        with VerticalScroll(id="panel-scroll"):
            yield _PanelContent(self, id="panel-content")
        yield _PreviewPane(id="preview-pane")

    def on_mount(self) -> None:
        self.set_interval(_REFRESH_INTERVAL, self._refresh_live)

    # ── public API ───────────────────────────────────────────────────────────

    @property
    def panel_type(self) -> str:
        return self._panel_type

    @property
    def preview_visible(self) -> bool:
        return self._preview_visible

    def cycle(self, delta: int) -> None:
        """Advance (delta=+1) or retreat (delta=-1) through tabs."""
        tabs = self.query_one("#panel-tabs", Tabs)
        if delta > 0:
            tabs.action_next_tab()
        else:
            tabs.action_previous_tab()

    def focus_tabs(self) -> None:
        try:
            self.query_one("#panel-tabs", Tabs).focus()
        except Exception:
            pass

    def cycle_event_filter(self) -> None:
        """Rotate through event filter groups; only meaningful on events tab."""
        self._event_filter_idx = (self._event_filter_idx + 1) % len(_FILTER_GROUPS)
        self._invalidate()

    def cycle_event_tail(self) -> None:
        """Rotate through tail-N values; only meaningful on events tab."""
        self._event_tail_idx = (self._event_tail_idx + 1) % len(_TAIL_CYCLE)
        self._invalidate()

    # ── tab activation ───────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        if event.key == "tab":
            event.prevent_default()
            event.stop()
            self.cycle(+1)
        elif event.key == "shift+tab":
            event.prevent_default()
            event.stop()
            self.cycle(-1)
        elif event.key == "enter":
            if self._panel_type == "docs":
                event.prevent_default()
                self._toggle_preview()
        elif event.key in ("n", "j"):
            if self._panel_type == "docs":
                event.prevent_default()
                self._docs_move(+1)
        elif event.key in ("p", "k"):
            if self._panel_type == "docs":
                event.prevent_default()
                self._docs_move(-1)
        elif event.key == "up":
            if self._preview_visible:
                event.prevent_default()
                self._scroll_preview(-1)
        elif event.key == "down":
            if self._preview_visible:
                event.prevent_default()
                self._scroll_preview(+1)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab and event.tab.id in PANEL_TYPES:
            self._panel_type = event.tab.id
            self._invalidate()
            if self._preview_visible:
                self._update_preview()

    # ── content refresh ──────────────────────────────────────────────────────

    def _refresh_live(self) -> None:
        if self._panel_type in _LIVE_PANELS:
            self._invalidate()

    def _invalidate(self) -> None:
        try:
            self.query_one("#panel-content", _PanelContent).invalidate()
        except Exception:
            pass

    # ── preview pane ─────────────────────────────────────────────────────────

    def _toggle_preview(self) -> None:
        self._preview_visible = not self._preview_visible
        try:
            pane = self.query_one("#preview-pane", _PreviewPane)
            if self._preview_visible:
                pane.add_class("preview-visible")
                self._update_preview()
            else:
                pane.remove_class("preview-visible")
        except Exception:
            pass

    def _update_preview(self) -> None:
        try:
            pane = self.query_one("#preview-pane", _PreviewPane)
            if self._panel_type == "docs" and self._docs_files:
                pane.show_markdown(self._docs_files[self._docs_cursor])
            else:
                pane.clear()
        except Exception:
            pass

    def _scroll_preview(self, delta: int) -> None:
        try:
            self.query_one("#preview-pane", _PreviewPane).scroll_line(delta)
        except Exception:
            pass

    # ── docs navigation ───────────────────────────────────────────────────────

    def _docs_move(self, delta: int) -> None:
        if not self._docs_files:
            return
        self._docs_cursor = max(0, min(len(self._docs_files) - 1, self._docs_cursor + delta))
        self._invalidate()
        if self._preview_visible:
            self._update_preview()
        self._scroll_docs_into_view()

    def _docs_cursor_y(self) -> int:
        """Return the Y coordinate (VerticalScroll space) of the cursor line.

        Structure of rendered lines:
          0: header
          1: blank
          per section: [section_header, file0, file1, ..., blank]
        #panel-content has padding-top:1, so add 1 for the scroll coordinate.
        """
        line = 2  # past header + blank
        file_idx = 0
        for section in sorted(self._docs_groups):
            line += 1  # section header
            for _md in self._docs_groups[section]:
                if file_idx == self._docs_cursor:
                    return 1 + line  # 1 = padding-top
                line += 1
                file_idx += 1
            line += 1  # trailing blank per section
        return 3  # fallback: near top

    def _scroll_docs_into_view(self) -> None:
        """Scroll #panel-scroll so the cursor line is visible."""
        y = self._docs_cursor_y()
        try:
            vs = self.query_one("#panel-scroll", VerticalScroll)
            current = int(vs.scroll_y)
            visible = vs.size.height
            if visible <= 0:
                return
            if y < current:
                vs.scroll_to(y=y, animate=False)
            elif y >= current + visible:
                vs.scroll_to(y=y - visible + 1, animate=False)
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
            if self._panel_type == "cost":
                return self._render_cost()
            if self._panel_type == "docs":
                return self._render_docs()
        except Exception as e:
            return f"[red]error: {_esc(str(e))}[/red]"
        return ""

    # ── panel renderers ──────────────────────────────────────────────────────

    def _render_keys(self) -> str:
        lines = ["[bold #C8553D]Key Bindings[/]", ""]
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
        if self._project_root is None:
            return "[bold #C8553D]Recent Events[/]\n\n[#555555]  (no project root)[/]"

        events_root = self._project_root / ".reyn" / "events"
        if not events_root.is_dir():
            return "[bold #C8553D]Recent Events[/]\n\n[#555555]  (no events yet)[/]"

        all_events: list[dict] = []
        for jsonl in sorted(events_root.rglob("*.jsonl")):
            try:
                for raw in jsonl.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if raw:
                        try:
                            all_events.append(json.loads(raw))
                        except Exception:
                            pass
            except Exception:
                pass

        filter_name, filter_set = _FILTER_GROUPS[self._event_filter_idx]
        tail = _TAIL_CYCLE[self._event_tail_idx]

        if filter_set:
            visible = [ev for ev in all_events if ev.get("type") in filter_set]
        else:
            visible = all_events

        filter_label = (
            f"[bold #C8553D]{filter_name}[/]" if filter_name != "all"
            else "[#555555]all[/]"
        )
        header = (
            f"[bold #C8553D]Recent Events[/]"
            f"  [#555555]filter:[/] {filter_label}"
            f"  [#555555]tail:[/] [#aaaaaa]{tail}[/]"
            f"  [#555555]({len(visible)}/{len(all_events)})[/]"
            f"  [#555555]f=filter  t=tail[/]"
        )

        if not visible:
            return header + "\n\n[#555555]  (no matching events)[/]"

        chain_replies = _load_chain_replies(self._project_root)

        lines = [header, ""]
        for ev in visible[-tail:][::-1]:
            ts = _esc(str(ev.get("timestamp", ""))[:19].replace("T", " "))
            ev_type = ev.get("type", "?")
            color = _EVENT_COLORS.get(ev_type, _DEFAULT_EVENT_COLOR)
            hint = _esc(_event_hint(ev))
            hint_part = f"  [#555555]{hint}[/]" if hint else ""
            lines.append(
                f"[#444444]  {ts}[/]  [{color}]{_esc(ev_type)}[/]{hint_part}"
            )
            if ev_type == "user_message_received":
                cid = (ev.get("data") or {}).get("chain_id")
                if cid:
                    reply = chain_replies.get(cid)
                    if reply is None:
                        lines.append("[#444444]       ↳ [/][#555555](awaiting…)[/]")
                    else:
                        short = _esc(reply[:72]) + ("…" if len(reply) > 72 else "")
                        lines.append(f"[#444444]       ↳ [/][#777777]{short}[/]")

        return "\n".join(lines)

    def _render_agents(self) -> str:
        lines = ["[bold #C8553D]Agents[/]", ""]

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
        lines = ["[bold #C8553D]Memory[/]", ""]

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
            lines.append(f"[bold {color}]  \\[{type_key.upper()}][/]")
            for e in group:
                lines.append(f"[#dddddd]    {_esc(e.name)}[/]")
                if e.description:
                    lines.append(f"[#555555]      {_esc(e.description)}[/]")
            lines.append("")

        if other:
            lines.append("[bold #888888]  \\[OTHER][/]")
            for e in other:
                lines.append(f"[#dddddd]    {_esc(e.name)}[/]")

        return "\n".join(lines)

    def _render_cost(self) -> str:
        import datetime
        from collections import defaultdict

        lines = ["[bold #C8553D]Cost[/]", ""]

        if self._project_root is None:
            lines.append("[#555555]  (no project root)[/]")
            return "\n".join(lines)

        events_root = self._project_root / ".reyn" / "events"
        if not events_root.is_dir():
            lines.append("[#555555]  (no events yet)[/]")
            return "\n".join(lines)

        today_str = datetime.date.today().isoformat()
        today = {"p": 0, "c": 0, "cost": 0.0, "calls": 0, "has_cost": False}
        total = {"p": 0, "c": 0, "cost": 0.0, "calls": 0, "has_cost": False}
        by_model: dict[str, dict] = defaultdict(
            lambda: {"p": 0, "c": 0, "cost": 0.0, "calls": 0, "has_cost": False}
        )

        for jsonl in sorted(events_root.rglob("*.jsonl")):
            try:
                pending_model: str = "unknown"
                for raw in jsonl.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                        ev_type = ev.get("type")
                        d = ev.get("data") or {}
                        if ev_type == "llm_called":
                            pending_model = str(d.get("model", "unknown"))
                            continue
                        if ev_type != "llm_response_received":
                            continue
                        pt = int(d.get("prompt_tokens", 0) or 0)
                        ct = int(d.get("completion_tokens", 0) or 0)
                        raw_cost = d.get("cost_usd")
                        cost = float(raw_cost) if raw_cost is not None else 0.0
                        has_cost = raw_cost is not None
                        model = pending_model
                        ts = str(ev.get("timestamp", ""))
                        for bucket in (total, by_model[model]):
                            bucket["p"] += pt; bucket["c"] += ct
                            bucket["cost"] += cost; bucket["calls"] += 1
                            if has_cost:
                                bucket["has_cost"] = True
                        if ts.startswith(today_str):
                            today["p"] += pt; today["c"] += ct
                            today["cost"] += cost; today["calls"] += 1
                            if has_cost:
                                today["has_cost"] = True
                    except Exception:
                        pass
            except Exception:
                pass

        def _cost_str(bucket: dict) -> str:
            if not bucket["has_cost"]:
                return "[#555555]N/A[/]"
            return f"[#44cc88]${bucket['cost']:.4f}[/]"

        def _tok(p: int, c: int) -> str:
            return f"[#dddddd]{p + c:,}[/] [#555555]({p:,}p + {c:,}c)[/]"

        lines.append("[bold #aaaaaa]  TODAY[/]")
        if today["calls"] == 0:
            lines.append("[#555555]    (no calls today)[/]")
        else:
            lines.append(f"[#555555]    tokens  [/]{_tok(today['p'], today['c'])}")
            lines.append(f"[#555555]    cost    [/]{_cost_str(today)}")
            lines.append(f"[#555555]    calls   [/][#dddddd]{today['calls']}[/]")
        lines.append("")

        lines.append("[bold #aaaaaa]  ALL TIME[/]")
        if total["calls"] == 0:
            lines.append("[#555555]    (no LLM calls)[/]")
        else:
            lines.append(f"[#555555]    tokens  [/]{_tok(total['p'], total['c'])}")
            lines.append(f"[#555555]    cost    [/]{_cost_str(total)}")
            lines.append(f"[#555555]    calls   [/][#dddddd]{total['calls']}[/]")
        lines.append("")

        if by_model:
            lines.append("[bold #aaaaaa]  BY MODEL[/]")
            for model in sorted(by_model):
                m = by_model[model]
                cost_part = f"  {_cost_str(m)}" if m["has_cost"] else ""
                lines.append(
                    f"[#dddddd]    {_esc(model)}[/]\n"
                    f"[#555555]      {m['p'] + m['c']:,} tok"
                    f"{cost_part}  {m['calls']} calls[/]"
                )

        return "\n".join(lines)

    def _render_docs(self) -> str:
        nav_hint = "[#555555]n/j↓  p/k↑  enter=open[/]"
        header = f"[bold #C8553D]Docs[/]  {nav_hint}"

        if self._project_root is None:
            return header + "\n\n[#555555]  (no project root)[/]"

        docs_root = self._project_root / "docs" / "en"
        if not docs_root.is_dir():
            return header + "\n\n[#555555]  (docs/en/ not found)[/]"

        # Build groups; files within each section retain sort order.
        groups: dict[str, list[Path]] = {}
        for md in sorted(docs_root.rglob("*.md")):
            rel = md.relative_to(docs_root)
            section = rel.parts[0] if len(rel.parts) > 1 else ""
            groups.setdefault(section, []).append(md)
        self._docs_groups = groups

        # Build flat list in render order so cursor index matches visual position.
        ordered: list[Path] = []
        for section in sorted(groups):
            ordered.extend(groups[section])
        self._docs_files = ordered
        if self._docs_cursor >= len(ordered):
            self._docs_cursor = max(0, len(ordered) - 1)

        lines = [header, ""]
        file_idx = 0
        for section in sorted(groups):
            label = section.upper() if section else "ROOT"
            lines.append(f"[bold #aaaaaa]  \\[{_esc(label)}][/]")
            for md in groups[section]:
                rel = md.relative_to(docs_root)
                depth = len(rel.parts) - 1
                indent = "    " + "  " * max(0, depth - 1)
                if file_idx == self._docs_cursor:
                    lines.append(f"[bold #C8553D]{indent}▶ {_esc(md.stem)}[/]")
                else:
                    lines.append(f"[#666666]{indent}  {_esc(md.stem)}[/]")
                file_idx += 1
            lines.append("")

        return "\n".join(lines)
