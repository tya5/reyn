"""RightPanel — swappable right-side panel slot for the Reyn TUI."""
from __future__ import annotations

_CORAL = "#C8553D"  # primary theme colour — matches Theme(primary=...)

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Group as RichGroup
from rich.text import Text as RichText
from rich.tree import Tree as RichTree
from textual.app import ComposeResult, RenderResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Label, RichLog, Static, Tab, Tabs
from textual.widgets._tabs import Underline as _Underline


class _TopTabs(Tabs):
    """Tabs with the underline indicator docked to the top instead of the bottom."""

    def on_mount(self) -> None:
        # Inline styles have highest priority — overrides DEFAULT_CSS dock:bottom
        self.query_one(_Underline).styles.dock = "top"

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


class _PanelHeader(Static):
    """Fixed header strip — 1 line of text + symmetric padding = natural 3 rows."""

    DEFAULT_CSS = """
    _PanelHeader {
        background: #1a1a1a;
        color: #aaaaaa;
        padding: 0 2;
        border-bottom: solid #333333;
    }
    """

    def __init__(self, panel: "RightPanel", **kwargs) -> None:
        super().__init__("", **kwargs)
        self._panel = panel

    def render(self) -> RenderResult:
        try:
            return self._panel._panel_header_markup()
        except Exception:
            return ""

    def invalidate(self) -> None:
        self._layout_cache.clear()
        self.refresh()


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
        border-top: tall $primary;
    }
    _PreviewPane:focus #preview-header {
        color: $primary;
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
        if event.key == "j":
            event.prevent_default()
            event.stop()
            self.scroll_line(+1)
        elif event.key == "k":
            event.prevent_default()
            event.stop()
            self.scroll_line(-1)
        elif event.key == "l":
            event.prevent_default()
            event.stop()
            self.scroll_col(+1)
        elif event.key == "h":
            event.prevent_default()
            event.stop()
            self.scroll_col(-1)

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
            log.scroll_to(y=log.scroll_y + delta, animate=False)
        except Exception:
            pass

    def scroll_col(self, delta: int) -> None:
        try:
            log = self.query_one("#preview-log", RichLog)
            log.scroll_to(x=log.scroll_x + delta, animate=False)
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
                f"  {name}  │  j↓  k↑  h←  l→"
            )
        except Exception:
            pass


class _TabContent(Widget):
    """Container below the tab bar: header + scroll area + preview pane."""

    DEFAULT_CSS = """
    _TabContent {
        height: 1fr;
        layout: vertical;
    }
    """


class RightPanel(Widget):
    """Swappable right-side panel with tab bar.

    Toggled open/close by ctrl+b.
    Tab cycling: ctrl+o (next) / ctrl+shift+o (prev, terminal-dependent).
    """

    DEFAULT_CSS = """
    RightPanel {
        display: none;
        width: 33%;
        border-left: tall #2a2a2a;
        background: #111111;
        layout: vertical;
        height: 100%;
    }

    RightPanel Tabs {
        background: #1a1a1a;
        height: 3;
    }

    RightPanel Tab {
        color: #666666;
        padding: 0 2;
    }

    RightPanel Tab.-active {
        color: $primary;
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
        self._panel_width: int = 0  # 0 = use CSS default (33%); set on first resize
        # run_id → {skill_name, agent_name, start_time, phase, phase_visits}
        self._exec_state: dict[str, dict] = {}

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with _TabContent(id="tab-content"):
            yield _PanelHeader(self, id="panel-header")
            with VerticalScroll(id="panel-scroll"):
                yield _PanelContent(self, id="panel-content")
            yield _PreviewPane(id="preview-pane")
        yield _TopTabs(
            *[Tab(_PANEL_LABELS[t], id=t) for t in PANEL_TYPES],
            id="panel-tabs",
        )

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
        tabs = self.query_one("#panel-tabs", _TopTabs)
        if delta > 0:
            tabs.action_next_tab()
        else:
            tabs.action_previous_tab()

    def focus_tabs(self) -> None:
        try:
            self.query_one("#panel-tabs", _TopTabs).focus()
        except Exception:
            pass

    def resize(self, delta: int) -> None:
        self._panel_resize(delta)

    def update_exec_state(self, state: dict[str, dict]) -> None:
        """Receive a live snapshot of running skills from the TUI app.

        Called from app._push_exec_state() whenever a trace event arrives.
        Triggers a re-render only when the agents tab is visible.
        """
        self._exec_state = dict(state)
        if self._panel_type == "agents":
            self._invalidate()

    def _panel_resize(self, delta: int) -> None:
        if self._panel_width == 0:
            self._panel_width = self.size.width or 40
        max_width = max(40, int((self.app.size.width or 120) * 0.66))
        self._panel_width = max(24, min(max_width, self._panel_width + delta))
        self.styles.width = self._panel_width

    def _scroll_panel(self, delta: int) -> None:
        try:
            vs = self.query_one("#panel-scroll", VerticalScroll)
            vs.scroll_to(y=vs.scroll_y + delta, animate=False)
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
        elif event.key == "j":
            event.prevent_default()
            if self._panel_type == "docs":
                self._docs_move(+1)
            else:
                self._scroll_panel(+1)
        elif event.key == "k":
            event.prevent_default()
            if self._panel_type == "docs":
                self._docs_move(-1)
            else:
                self._scroll_panel(-1)
        elif event.key == "l":
            event.prevent_default()
            self._panel_resize(-2)
        elif event.key == "h":
            event.prevent_default()
            self._panel_resize(+2)


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
            self.query_one("#panel-header", _PanelHeader).invalidate()
        except Exception:
            pass
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

    def _panel_header_markup(self) -> str:
        if self._panel_type == "keys":
            return f"[bold {_CORAL}]Key Bindings[/]"
        if self._panel_type == "agents":
            return f"[bold {_CORAL}]Agents[/]"
        if self._panel_type == "memory":
            return f"[bold {_CORAL}]Memory[/]"
        if self._panel_type == "cost":
            return f"[bold {_CORAL}]Cost[/]"
        if self._panel_type == "docs":
            return f"[bold {_CORAL}]Docs[/]  [#555555]j↓  k↑  enter=open[/]"
        if self._panel_type == "events":
            filter_name, _ = _FILTER_GROUPS[self._event_filter_idx]
            tail = _TAIL_CYCLE[self._event_tail_idx]
            filter_label = (
                f"[bold {_CORAL}]{filter_name}[/]" if filter_name != "all"
                else "[#555555]all[/]"
            )
            lbr = "[#555555]\\[[/]"
            rbr = "[#555555]][/]"
            kf = f"{lbr}[{_CORAL}]f[/]{rbr}"
            kt = f"{lbr}[{_CORAL}]t[/]{rbr}"
            return (
                f"[bold {_CORAL}]Events[/]"
                f"  {kf}[#555555]ilter:[/]{filter_label}"
                f"  {kt}[#555555]ail:[/][#aaaaaa]{tail}[/]"
            )
        return ""

    def _panel_markup(self) -> Any:
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
        lines: list[str] = []
        seen: set[str] = set()
        for raw in self.app.BINDINGS:
            b = raw if isinstance(raw, Binding) else Binding(*raw)
            if b.key in seen or not b.description:
                continue
            seen.add(b.key)
            key_col = f"{_esc(b.key):<18}"
            desc_col = _esc(b.description)
            lines.append(f"[#aaaaaa]  {key_col}[/]  [#dddddd]{desc_col}[/]")
        if not lines:
            lines.append("[#555555]  (no bindings)[/]")
        return "\n".join(lines)

    def _render_events(self) -> str:
        if self._project_root is None:
            return "[#555555]  (no project root)[/]"

        events_root = self._project_root / ".reyn" / "events"
        if not events_root.is_dir():
            return "[#555555]  (no events yet)[/]"

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
            f"[bold {_CORAL}]{filter_name}[/]" if filter_name != "all"
            else "[#555555]all[/]"
        )
        header = (
            f"[bold {_CORAL}]Recent Events[/]"
            f"  [#555555]filter:[/] {filter_label}"
            f"  [#555555]tail:[/] [#aaaaaa]{tail}[/]"
            f"  [#555555]({len(visible)}/{len(all_events)})[/]"
            f"  [#555555]f=filter  t=tail[/]"
        )
        if not visible:
            return "[#555555]  (no matching events)[/]"

        chain_replies = _load_chain_replies(self._project_root)

        lines: list[str] = []
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

    def _render_agents(self) -> Any:
        import time as _time

        if self._registry is None:
            return "[#555555]  (no registry)[/]"

        names = self._registry.list_names()
        if not names:
            return "[#555555]  (no agents)[/]"

        attached = self._registry.attached_name
        loaded = set(self._registry.loaded_names())
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
                for rid, info in self._exec_state.items()
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
                    last = self._registry.last_activity_at(name)
                    if last:
                        ts = last.strftime("%Y-%m-%d %H:%M")
                        tree.add(RichText(f"last: {ts}", style="#555555"))
                except Exception:
                    pass

            agent_trees.append(tree)

        # interleave blank lines between agent blocks
        items: list[Any] = []
        for i, tree in enumerate(agent_trees):
            if i > 0:
                items.append(RichText(""))
            items.append(tree)
        return RichGroup(*items)

    def _render_memory(self) -> str:
        if self._project_root is None:
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
        shared = list_entries(self._project_root / ".reyn" / "memory")
        _render_scope(shared, "SHARED", _CORAL)

        # Per-agent memory
        agents_dir = self._project_root / ".reyn" / "agents"
        if agents_dir.exists():
            for agent_dir in sorted(agents_dir.iterdir()):
                mem_dir = agent_dir / "memory"
                if not mem_dir.exists():
                    continue
                agent_entries = list_entries(mem_dir)
                _render_scope(agent_entries, f"AGENT  {agent_dir.name}", "#7a9fc7")

        return "\n".join(lines)

    def _render_cost(self) -> str:
        import datetime
        from collections import defaultdict

        lines: list[str] = []

        if self._project_root is None:
            lines.append("[#555555]  (no project root)[/]")
            return "\n".join(lines)

        events_root = self._project_root / ".reyn" / "events"
        if not events_root.is_dir():
            lines.append("[#555555]  (no events yet)[/]")
            return "\n".join(lines)

        today_str = datetime.date.today().isoformat()

        def _new_bucket() -> dict:
            return {"p": 0, "c": 0, "cost": 0.0, "calls": 0,
                    "has_cost": False, "call_costs": []}

        today = _new_bucket()
        total = _new_bucket()
        by_agent: dict[str, dict] = defaultdict(_new_bucket)
        # agent → skill → bucket
        by_agent_skill: dict[str, dict[str, dict]] = defaultdict(
            lambda: defaultdict(_new_bucket)
        )

        for jsonl in sorted(events_root.rglob("*.jsonl")):
            try:
                rel = jsonl.relative_to(events_root)
                parts = rel.parts
                # agent attribution from path: agents/<name>/skill_runs/...
                if parts[0] == "agents" and len(parts) >= 2:
                    agent = parts[1]
                elif parts[0] == "direct":
                    agent = "direct"
                else:
                    agent = "?"
                # skill name from filename suffix (only for skill_runs files)
                is_skill_run = "skill_runs" in parts
                if is_skill_run:
                    stem = jsonl.stem  # e.g. "2026-05-04T120000_skill_router"
                    skill = stem.split("_", 1)[1] if "_" in stem else stem
                else:
                    skill = "(chat)"

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
                        ts = str(ev.get("timestamp", ""))

                        for bucket in (total, by_agent[agent], by_agent_skill[agent][skill]):
                            bucket["p"] += pt; bucket["c"] += ct
                            bucket["cost"] += cost; bucket["calls"] += 1
                            if has_cost:
                                bucket["has_cost"] = True
                            bucket["call_costs"].append(cost)

                        if ts.startswith(today_str):
                            today["p"] += pt; today["c"] += ct
                            today["cost"] += cost; today["calls"] += 1
                            if has_cost:
                                today["has_cost"] = True
                            today["call_costs"].append(cost)
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

        def _sparkline(values: list[float], width: int = 32) -> str:
            if not values:
                return ""
            recent = values[-width:]
            max_v = max(recent) or 1
            blocks = "▁▂▃▄▅▆▇█"
            bar = "".join(blocks[min(7, int(v / max_v * 8))] for v in recent)
            return f"[{_CORAL}]{bar}[/]"

        # ── TODAY ────────────────────────────────────────────────────────
        lines.append("[bold #aaaaaa]  TODAY[/]")
        if today["calls"] == 0:
            lines.append("[#555555]    (no calls today)[/]")
        else:
            lines.append(f"[#555555]    tokens  [/]{_tok(today['p'], today['c'])}")
            lines.append(f"[#555555]    cost    [/]{_cost_str(today)}")
            lines.append(f"[#555555]    calls   [/][#dddddd]{today['calls']}[/]")
            spark = _sparkline(today["call_costs"])
            if spark:
                lines.append(f"[#555555]    trend   [/]{spark}")
        lines.append("")

        # ── ALL TIME ──────────────────────────────────────────────────────
        lines.append("[bold #aaaaaa]  ALL TIME[/]")
        if total["calls"] == 0:
            lines.append("[#555555]    (no LLM calls)[/]")
        else:
            lines.append(f"[#555555]    tokens  [/]{_tok(total['p'], total['c'])}")
            lines.append(f"[#555555]    cost    [/]{_cost_str(total)}")
            lines.append(f"[#555555]    calls   [/][#dddddd]{total['calls']}[/]")
            spark = _sparkline(total["call_costs"])
            if spark:
                lines.append(f"[#555555]    trend   [/]{spark}")
        lines.append("")

        # ── BY AGENT / SKILL ──────────────────────────────────────────────
        lines.append("[bold #aaaaaa]  BY AGENT / SKILL[/]")
        if by_agent_skill:
            for agent in sorted(by_agent_skill):
                ag = by_agent[agent]
                ag_tok = ag["p"] + ag["c"]
                # agent total: name bold white, tok light gray, cost bright green
                ag_cost = (
                    f"  [bold #44cc88]${ag['cost']:.4f}[/]"
                    if ag["has_cost"] else ""
                )
                # name col = 26 chars (2 indent + 24) to align with skill rows (4 + 22)
                lines.append(
                    f"[bold #dddddd]  {_esc(agent):<24}[/]"
                    f"[#aaaaaa]{ag_tok:>7,} tok[/]"
                    f"{ag_cost}"
                    f"  [#777777]{ag['calls']}c[/]"
                )
                skills = by_agent_skill[agent]
                for skill in sorted(skills):
                    m = skills[skill]
                    tok_total = m["p"] + m["c"]
                    # skill rows: dim name, muted green for cost — clearly subordinate
                    cost_part = (
                        f"  [#2d7a4f]${m['cost']:.4f}[/]"
                        if m["has_cost"] else ""
                    )
                    lines.append(
                        f"[#555555]    {_esc(skill):<22}[/]"
                        f"[#555555]{tok_total:>7,} tok[/]"
                        f"{cost_part}"
                        f"  [#444444]{m['calls']}c[/]"
                    )
                lines.append("")
        else:
            lines.append("[#555555]    (no skill runs yet)[/]")

        return "\n".join(lines)

    def _render_docs(self) -> str:
        if self._project_root is None:
            return "[#555555]  (no project root)[/]"

        docs_root = self._project_root / "docs" / "en"
        if not docs_root.is_dir():
            return "[#555555]  (docs/en/ not found)[/]"

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

        lines: list[str] = []
        file_idx = 0
        for section in sorted(groups):
            label = section.upper() if section else "ROOT"
            lines.append(f"[bold #aaaaaa]  \\[{_esc(label)}][/]")
            for md in groups[section]:
                rel = md.relative_to(docs_root)
                indent = "    "
                if file_idx == self._docs_cursor:
                    lines.append(f"[bold {_CORAL}]{indent}▶ {_esc(md.stem)}[/]")
                else:
                    lines.append(f"[#666666]{indent}  {_esc(md.stem)}[/]")
                file_idx += 1
            lines.append("")

        return "\n".join(lines)
