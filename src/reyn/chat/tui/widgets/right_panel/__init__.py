"""RightPanel — swappable right-side panel slot for the Reyn TUI.

This module hosts the Textual widget shell (compose / mount / focus / key
bindings / refresh loop) and dispatches per-tab rendering to dedicated
sibling modules (``keys_tab``, ``events_tab``, ``agents_tab``, ``memory_tab``,
``cost_tab``, ``docs_tab``). The split keeps each tab's rendering logic
independently readable while preserving the public API: ``RightPanel`` and
``PANEL_TYPES`` are still importable from ``reyn.chat.tui.widgets``.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Tab, Tabs

from .agents_tab import render_agents
from .base import _CORAL, _esc, logger
from .cost_tab import render_cost
from .docs_tab import build_docs_index, render_docs
from .events_tab import _FILTER_GROUPS, _TAIL_CYCLE, render_events
from .keys_tab import render_keys
from .memory_tab import render_memory
from .shells import (
    _PanelContent,
    _PanelHeader,
    _PanelTop,
    _PreviewPane,
    _TabContent,
)

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

_LIVE_PANELS = {"events", "agents", "cost"}
_REFRESH_INTERVAL = 2.0


class RightPanel(Widget):
    """Swappable right-side panel with tab bar.

    Toggled open/close by ctrl+b.
    Tab cycling: ctrl+o (next) / ctrl+shift+o (prev, terminal-dependent).
    """

    DEFAULT_CSS = """
    RightPanel {
        display: none;
        width: 33%;
        background: #111111;
        layout: vertical;
        height: 100%;
    }

    /* Tabs at the top of the panel via compose-order. Background uses the
       panel's `#111111` (vs the screen header's `#1a1a1a`) so the boundary
       between header and tabs is visible from colour delta alone. */
    RightPanel Tabs {
        background: #111111;
        border-left: solid #2a2a2a;
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

    RightPanel #panel-scroll {
        height: 1fr;
    }

    RightPanel #panel-content {
        height: auto;
        color: #666666;
        padding: 0 1;
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
        self._docs_filter: str = ""
        self._preview_visible: bool = False
        self._panel_width: int = 0  # 0 = use CSS default (33%); set on first resize
        # run_id → {skill_name, agent_name, start_time, phase, phase_visits}
        self._exec_state: dict[str, dict] = {}
        # Events tab state — mtime-based parse cache + cursor + visible-list cache
        self._events_cache: dict[Path, tuple[float, list[dict]]] = {}
        self._events_cursor: int = 0
        self._events_visible: list[dict] = []
        # Memory tab state — flat cursor over all entries
        self._memory_cursor: int = 0
        self._memory_entries: list[Any] = []

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # Tabs first (top of panel) — vertical compose order = render order.
        yield Tabs(
            *[Tab(_PANEL_LABELS[t], id=t) for t in PANEL_TYPES],
            id="panel-tabs",
        )
        with _TabContent(id="tab-content"):
            with _PanelTop(id="panel-top"):
                yield _PanelHeader(self, id="panel-header")
                with VerticalScroll(id="panel-scroll"):
                    yield _PanelContent(self, id="panel-content")
            yield _PreviewPane(id="preview-pane")

    def on_mount(self) -> None:
        self.set_interval(_REFRESH_INTERVAL, self._refresh_live)

    # ── focus indicator (X-focused on _PanelTop when tabs hold focus) ───────

    def on_descendant_focus(self, event) -> None:
        """A descendant gained focus — refresh the x-focused class on X."""
        self._update_x_focused()

    def on_descendant_blur(self, event) -> None:
        """A descendant lost focus — re-evaluate (focus may have left panel)."""
        self._update_x_focused()

    def _update_x_focused(self) -> None:
        """Light up _PanelTop's coral ring when (and only when) `#panel-tabs`
        holds focus. Preview pane has its own :focus CSS; tabs themselves
        stay gray — the eye is drawn upward to the content the tabs represent.
        """
        try:
            top = self.query_one("#panel-top", _PanelTop)
            tabs = self.query_one("#panel-tabs", Tabs)
        except Exception:
            return
        focused = self.app.focused
        if focused is None:
            top.set_class(False, "x-focused")
            return
        is_tabs = focused is tabs or any(a is tabs for a in focused.ancestors)
        top.set_class(is_tabs, "x-focused")

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
        except Exception as exc:
            logger.warning("right_panel focus_tabs failed: %s", exc)

    def set_panel_type(self, panel_type: str) -> None:
        """Programmatically switch to a specific tab (e.g. 'events' / 'agents').

        Used by ReynTUIApp.action_toggle_panel for smart Ctrl+B targeting and
        by /docs-filter to jump the user to the docs tab.
        """
        if panel_type not in PANEL_TYPES:
            return
        try:
            tabs = self.query_one("#panel-tabs", Tabs)
            tabs.active = panel_type
        except Exception as exc:
            logger.warning("right_panel set_panel_type failed: %s", exc)

    def set_docs_filter(self, substr: str) -> None:
        """Set the docs-tab substring filter; empty clears."""
        self._docs_filter = (substr or "").strip()
        self._docs_cursor = 0
        self._invalidate()

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
        except Exception as exc:
            logger.warning("right_panel scroll_panel failed: %s", exc)

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
        elif event.key == "space":
            # Space toggles / opens the preview pane on docs / events /
            # memory tabs. Enter is reserved for keystroke-passthrough on
            # focused inputs (e.g. modal dialogs); Space is the standard
            # "activate the highlighted row" key in TUIs.
            if self._panel_type == "docs":
                event.prevent_default()
                self._toggle_preview()
            elif self._panel_type == "events":
                event.prevent_default()
                self._events_show_selected()
            elif self._panel_type == "memory":
                event.prevent_default()
                self._memory_show_selected()
        elif event.key == "j":
            event.prevent_default()
            if self._panel_type == "docs":
                self._docs_move(+1)
            elif self._panel_type == "events":
                self._events_move(+1)
            elif self._panel_type == "memory":
                self._memory_move(+1)
            else:
                self._scroll_panel(+1)
        elif event.key == "k":
            event.prevent_default()
            if self._panel_type == "docs":
                self._docs_move(-1)
            elif self._panel_type == "events":
                self._events_move(-1)
            elif self._panel_type == "memory":
                self._memory_move(-1)
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

    @on(_PreviewPane.CloseRequested)
    def _on_preview_close_requested(self, event) -> None:
        """Space pressed while the preview pane had focus — close the pane.

        After hiding, pull focus back to the tab strip so further j/k/space
        keystrokes are routed to the panel, not the now-hidden preview.
        """
        if self._preview_visible:
            self._toggle_preview()
        try:
            self.query_one("#panel-tabs", Tabs).focus()
        except Exception as exc:
            logger.warning("right_panel close-preview focus failed: %s", exc)

    # ── content refresh ──────────────────────────────────────────────────────

    def _refresh_live(self) -> None:
        if self._panel_type in _LIVE_PANELS:
            self._invalidate()

    def _invalidate(self) -> None:
        try:
            self.query_one("#panel-header", _PanelHeader).invalidate()
        except Exception as exc:
            logger.warning("right_panel header invalidate failed: %s", exc)
        try:
            self.query_one("#panel-content", _PanelContent).invalidate()
        except Exception as exc:
            logger.warning("right_panel content invalidate failed: %s", exc)

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
        except Exception as exc:
            logger.warning("right_panel toggle_preview failed: %s", exc)

    def _update_preview(self) -> None:
        try:
            pane = self.query_one("#preview-pane", _PreviewPane)
            if self._panel_type == "docs" and self._docs_files:
                pane.show_markdown(self._docs_files[self._docs_cursor])
            elif self._panel_type == "events" and self._events_visible:
                self._show_event_in_preview(pane)
            elif self._panel_type == "memory" and self._memory_entries:
                self._show_memory_in_preview(pane)
            else:
                pane.clear()
        except Exception as exc:
            logger.warning("right_panel update_preview failed: %s", exc)

    # ── events / memory cursor + preview integration ─────────────────────────

    def _events_move(self, delta: int) -> None:
        """Move the events tab cursor and refresh; sync preview if open."""
        if not self._events_visible:
            return
        self._events_cursor = max(
            0, min(len(self._events_visible) - 1, self._events_cursor + delta),
        )
        self._invalidate()
        if self._preview_visible:
            self._update_preview()

    def _events_show_selected(self) -> None:
        """Space on events tab — toggle preview pane (open with selected
        event, or close if already open)."""
        if not self._events_visible:
            return
        self._toggle_preview()
        if self._preview_visible:
            self._update_preview()

    def _show_event_in_preview(self, pane: _PreviewPane) -> None:
        """Render the cursor's event as JSON in the preview pane."""
        import json as _json
        if not self._events_visible:
            pane.clear()
            return
        idx = max(0, min(len(self._events_visible) - 1, self._events_cursor))
        ev = self._events_visible[idx]
        from rich.syntax import Syntax  # lazy — keeps cold-start fast
        title = f"event #{idx} · {ev.get('type', '?')}"
        try:
            body = _json.dumps(ev, indent=2, default=str, ensure_ascii=False)
        except Exception:
            body = repr(ev)
        try:
            renderable = Syntax(
                body, "json",
                theme="ansi_dark",
                background_color="default",
                line_numbers=False,
                word_wrap=True,
            )
        except Exception:
            from rich.text import Text as RichText
            renderable = RichText(body)
        pane.show_text(title, renderable)

    def _memory_move(self, delta: int) -> None:
        """Move the memory tab cursor; sync preview if open."""
        if not self._memory_entries:
            return
        self._memory_cursor = max(
            0, min(len(self._memory_entries) - 1, self._memory_cursor + delta),
        )
        self._invalidate()
        if self._preview_visible:
            self._update_preview()

    def _memory_show_selected(self) -> None:
        """Space on memory tab — toggle preview pane (open with selected
        entry, or close if already open)."""
        if not self._memory_entries:
            return
        self._toggle_preview()
        if self._preview_visible:
            self._update_preview()

    def _show_memory_in_preview(self, pane: _PreviewPane) -> None:
        """Render the cursor's memory entry's body as Markdown in the preview."""
        if not self._memory_entries:
            pane.clear()
            return
        idx = max(0, min(len(self._memory_entries) - 1, self._memory_cursor))
        entry = self._memory_entries[idx]
        from rich.console import Group as RichGroup
        from rich.markdown import Markdown as RichMarkdown
        from rich.text import Text as RichText
        head = RichText()
        head.append(getattr(entry, "name", "") or "", style="bold " + _CORAL)
        if getattr(entry, "type", ""):
            head.append("  ")
            head.append(f"[{entry.type}]", style="dim")
        if getattr(entry, "description", ""):
            head.append("\n")
            head.append(entry.description, style="#aaaaaa")
        head.append("\n")
        body_md = RichMarkdown(getattr(entry, "body", "") or "")
        title = getattr(entry, "slug", "") or getattr(entry, "name", "") or "memory"
        pane.show_text(title, RichGroup(head, body_md))

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
        except Exception as exc:
            logger.warning("right_panel scroll_docs_into_view failed: %s", exc)

    # ── render dispatch ───────────────────────────────────────────────────────

    def _panel_header_markup(self) -> str:
        if self._panel_type == "keys":
            return f"[bold {_CORAL}]Key Bindings[/]"
        if self._panel_type == "agents":
            return f"[bold {_CORAL}]Agents[/]"
        if self._panel_type == "memory":
            return f"[bold {_CORAL}]Memory[/]  [#555555]j↓  k↑  space=open[/]"
        if self._panel_type == "cost":
            return f"[bold {_CORAL}]Cost[/]"
        if self._panel_type == "docs":
            return f"[bold {_CORAL}]Docs[/]  [#555555]j↓  k↑  space=open[/]"
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
                f"  [#555555]j/k=move  space=open[/]"
            )
        return ""

    def _panel_markup(self) -> Any:
        try:
            if self._panel_type == "keys":
                return render_keys(self.app)
            if self._panel_type == "events":
                rendered, windowed = render_events(
                    self._project_root,
                    self._event_filter_idx,
                    self._event_tail_idx,
                    cursor=self._events_cursor,
                    cache=self._events_cache,
                )
                self._events_visible = windowed
                if self._events_cursor >= len(windowed):
                    self._events_cursor = max(0, len(windowed) - 1)
                return rendered
            if self._panel_type == "agents":
                return render_agents(self._registry, self._exec_state)
            if self._panel_type == "memory":
                rendered, flat_entries = render_memory(
                    self._project_root, cursor=self._memory_cursor,
                )
                self._memory_entries = flat_entries
                if self._memory_cursor >= len(flat_entries):
                    self._memory_cursor = max(0, len(flat_entries) - 1)
                return rendered
            if self._panel_type == "cost":
                budget_tracker = getattr(self.app, "_budget_tracker", None)
                return render_cost(self._project_root, budget_tracker)
            if self._panel_type == "docs":
                groups, ordered = build_docs_index(
                    self._project_root, self._docs_filter,
                )
                self._docs_groups = groups
                self._docs_files = ordered
                if self._docs_cursor >= len(ordered):
                    self._docs_cursor = max(0, len(ordered) - 1)
                return render_docs(
                    self._project_root, self._docs_cursor, groups,
                    docs_filter=self._docs_filter,
                )
        except Exception as e:
            logger.warning("right_panel render dispatch failed: %s", e)
            return f"[red]error: {_esc(str(e))}[/red]"
        return ""


__all__ = ["RightPanel", "PANEL_TYPES"]
