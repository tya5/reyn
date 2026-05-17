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
        padding: 0 0;
        margin: 0 1 0 0;
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
        self._event_tail_idx: int = 3  # default tail=200; phase/llm events buried past tail-30
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
        # File-list TTL cache: [] or [timestamp, [Path…]] — refreshed ≤ every 10s
        self._events_filelist_cache: list = []
        self._events_cursor: int = 0
        self._events_visible: list[dict] = []
        # Memory tab state — flat cursor over all entries
        self._memory_cursor: int = 0
        self._memory_entries: list[Any] = []
        # Agents tab state — same cursor pattern as events / memory.
        # `_agents_items` is the flat list returned by `render_agents`,
        # one entry per running skill / running plan / recent skill /
        # recent plan. Used by j/k navigation and preview rendering.
        self._agents_cursor: int = 0
        self._agents_items: list[dict] = []

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
        """Light up _PanelTop's coral ring whenever the upper-right panel
        region "owns" focus — i.e. `#panel-tabs` OR `_PanelTop` itself.

        Preview pane has its own :focus CSS; tabs themselves stay gray —
        the eye is drawn upward to the content the tabs represent.
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
        in_tabs = focused is tabs or any(a is tabs for a in focused.ancestors)
        in_top = focused is top or any(a is top for a in focused.ancestors)
        top.set_class(in_tabs or in_top, "x-focused")

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
        new_width = max(24, min(max_width, self._panel_width + delta))
        # Flash the new width in the conv sticky bar so the user sees
        # something changed (and learns the bounds via the at-min /
        # at-max suffix). Skip the flash on no-op clamps to avoid the
        # impression that the keystroke was lost when nothing moved.
        at_min = new_width == 24
        at_max = new_width == max_width
        clamp_hint = (
            " (min)" if at_min and delta < 0
            else " (max)" if at_max and delta > 0
            else ""
        )
        self._panel_width = new_width
        self.styles.width = self._panel_width
        self._flash_status(f"panel: {new_width} col{clamp_hint}")

    def _flash_status(self, text: str, *, duration: float = 1.5) -> None:
        """Brief sticky-status message in the conv pane, auto-hides.

        Used for transient feedback (panel resize, panel-action confirm)
        that doesn't warrant a persistent log entry. Falls through
        silently if the conv pane isn't reachable (e.g. headless tests).
        """
        from .. import ConversationView  # late import → avoid cycle
        try:
            conv = self.app.query_one("#conversation", ConversationView)
        except Exception as exc:
            logger.warning("right_panel flash status: conv query failed: %s", exc)
            return
        try:
            conv.show_status(text, kind="general")
            self.app.set_timer(duration, conv.hide_status)
        except Exception as exc:
            logger.warning("right_panel flash status failed: %s", exc)

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
            # Space is a uniform preview toggle: works on any tab when
            # focus is anywhere inside the right panel (tabs included).
            # _update_preview() dispatches per-tab content (docs/events/
            # memory); other tabs render a cleared preview pane.
            event.prevent_default()
            self._toggle_preview()
            if self._preview_visible:
                self._update_preview()
        elif event.key == "j":
            event.prevent_default()
            if self._panel_type == "docs":
                self._docs_move(+1)
            elif self._panel_type == "events":
                self._events_move(+1)
            elif self._panel_type == "memory":
                self._memory_move(+1)
            elif self._panel_type == "agents":
                self._agents_move(+1)
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
            elif self._panel_type == "agents":
                self._agents_move(-1)
            else:
                self._scroll_panel(-1)
        elif event.key == "l":
            event.prevent_default()
            self._panel_resize(-2)
        elif event.key == "h":
            event.prevent_default()
            self._panel_resize(+2)
        elif event.key == "c":
            # Generic copy: whatever the right panel is currently
            # showing — the preview pane content if it's visible,
            # otherwise the main upper-panel content for the active
            # tab. Designed for skill authors / debuggers to grab
            # whatever they're looking at and paste it into chat /
            # an issue tracker.
            event.prevent_default()
            self._copy_current_view()
        elif event.key == "/":
            # Docs tab only: pre-fill the conv input with the
            # `/docs-filter` slash command so the user can type a
            # substring and press Enter. Advertised by keys_tab._DOCS_KEYS
            # and the Docs header `/=filter` hint. MVP — no inline
            # filter buffer; just hand off to the existing slash command.
            if self._panel_type == "docs":
                event.prevent_default()
                self._prefill_docs_filter()


    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab and event.tab.id in PANEL_TYPES:
            self._panel_type = event.tab.id
            self._invalidate()
            if self._preview_visible:
                self._update_preview()

    @on(_PreviewPane.CloseRequested)
    def _on_preview_close_requested(self, event) -> None:
        """Space pressed while the preview pane had focus — close the pane.

        After hiding, focus moves to the upper-right panel region
        (``_PanelTop``). Key events bubble to ``RightPanel.on_key`` so
        j/k/space/Tab all keep working from there. The ``x-focused`` ring
        is preserved because ``_update_x_focused`` recognises both
        tabs-focused and _PanelTop-focused as "right-upper-panel active".
        """
        if self._preview_visible:
            self._toggle_preview()
        try:
            self.query_one("#panel-top", _PanelTop).focus()
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
            elif self._panel_type == "agents" and self._agents_items:
                self._show_agent_in_preview(pane)
            else:
                pane.clear()
        except Exception as exc:
            logger.warning("right_panel update_preview failed: %s", exc)

    # ── events / memory cursor + preview integration ─────────────────────────

    def _events_move(self, delta: int) -> None:
        """Move the events tab cursor and refresh; sync preview if open.

        Cursor wraps around modulo the list length — `j` past the last
        item returns to index 0, `k` from the top jumps to the last
        item (Vim-list behaviour).
        """
        n = len(self._events_visible)
        if n == 0:
            self._events_cursor = 0
            return
        self._events_cursor = (self._events_cursor + delta) % n
        self._invalidate()
        if self._preview_visible:
            self._update_preview()

    def _render_as_yaml(self, value: Any) -> Any:
        """Format ``value`` as a YAML block + Rich syntax highlighter.

        YAML is dramatically easier to scan than the equivalent JSON for
        the kind of nested structures we surface in the events / recent-
        skill previews — fewer braces / commas / quotes, multi-line
        strings stay multi-line, and the indented block style matches
        the way the events are mentally grouped (top-level type, then
        nested data / meta).

        Implementation: round-trip through ``json.dumps(default=str)``
        first to coerce non-YAML-native types (Path, datetime, custom
        classes) into plain strings; then ``yaml.safe_dump`` the result.
        Falls back to JSON, then to ``repr`` if either step explodes —
        the preview should never hide content because of a serialisation
        edge case.
        """
        import json as _json

        from rich.syntax import Syntax
        from rich.text import Text as RichText
        body: str
        try:
            import yaml as _yaml
            normalised = _json.loads(
                _json.dumps(value, default=str, ensure_ascii=False),
            )
            body = _yaml.safe_dump(
                normalised,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=120,
            )
        except Exception:
            try:
                body = _json.dumps(value, indent=2, default=str, ensure_ascii=False)
            except Exception:
                body = repr(value)
        try:
            return Syntax(
                body, "yaml",
                theme="ansi_dark",
                background_color="default",
                line_numbers=False,
                word_wrap=True,
            )
        except Exception:
            return RichText(body)

    def _show_event_in_preview(self, pane: _PreviewPane) -> None:
        """Render the cursor's event as YAML in the preview pane."""
        if not self._events_visible:
            pane.clear()
            return
        idx = max(0, min(len(self._events_visible) - 1, self._events_cursor))
        ev = self._events_visible[idx]
        title = f"event #{idx} · {ev.get('type', '?')}"
        pane.show_text(title, self._render_as_yaml(ev))

    def _memory_move(self, delta: int) -> None:
        """Move the memory tab cursor; sync preview if open.

        Cursor wraps around modulo the list length (Vim-list behaviour).
        """
        n = len(self._memory_entries)
        if n == 0:
            self._memory_cursor = 0
            return
        self._memory_cursor = (self._memory_cursor + delta) % n
        self._invalidate()
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

    # ── agents tab cursor + preview integration ──────────────────────────────

    def _agents_move(self, delta: int) -> None:
        """Move the agents tab cursor; sync preview if open.

        Cursor wraps around modulo the list length (Vim-list behaviour).
        """
        n = len(self._agents_items)
        if n == 0:
            self._agents_cursor = 0
            return
        self._agents_cursor = (self._agents_cursor + delta) % n
        self._invalidate()
        if self._preview_visible:
            self._update_preview()

    def _show_agent_in_preview(self, pane: _PreviewPane) -> None:
        """Render the cursor's agent-tab item in the preview pane.

        Each ``kind`` produces a different layout:

          * ``running_skill`` — agent / skill / current phase / elapsed +
            recent traces (read from the live skill_run jsonl if visible).
          * ``recent_skill``  — full event sequence from the run's jsonl
            file, JSON-prettified (mirrors the events tab preview).
          * ``running_plan``  — agent / plan_id / goal / done/total /
            failed count.
          * ``recent_plan``   — agent / plan_id / goal / completion stats /
            exception type when interrupted.
        """
        if not self._agents_items:
            pane.clear()
            return
        idx = max(0, min(len(self._agents_items) - 1, self._agents_cursor))
        item = self._agents_items[idx]
        kind = item.get("kind", "")
        if kind == "running_skill":
            self._preview_running_skill(pane, item)
        elif kind == "running_plan":
            self._preview_running_plan(pane, item)
        elif kind == "recent_skill":
            self._preview_recent_skill(pane, item)
        elif kind == "recent_plan":
            self._preview_recent_plan(pane, item)
        else:
            pane.clear()

    def _preview_running_skill(self, pane: _PreviewPane, item: dict) -> None:
        """Live snapshot for an in-flight skill run."""
        from rich.console import Group as RichGroup
        from rich.text import Text as RichText
        head = RichText()
        head.append(item.get("skill_name", "?"), style="bold " + _CORAL)
        head.append("  ")
        head.append("(running)", style="#44cc88")
        head.append("\n")
        head.append("agent: ", style="dim")
        head.append(item.get("agent", "?"), style="#dddddd")
        head.append("\n")
        head.append("run_id: ", style="dim")
        head.append(item.get("run_id", "?"), style="#dddddd")
        head.append("\n")
        head.append("elapsed: ", style="dim")
        head.append(f"{item.get('elapsed_s', 0)}s", style="#dddddd")
        head.append("\n")
        head.append("phase: ", style="dim")
        head.append(item.get("phase", "—") or "—", style="#dddddd")
        if item.get("phase_visits", 0) > 1:
            head.append(f"  (visit #{item['phase_visits']})", style="#888888")
        # The user message that kicked off this run. Lets the user
        # disambiguate same-named skills triggered by different turns —
        # the typical "why is web_search_display still running, didn't
        # I already get my answer?" gotcha (= different run from a
        # different prompt).
        trig = item.get("triggered_by", "") or ""
        if trig:
            head.append("\n\ntriggered by:\n", style="dim")
            # Truncate to 240 chars so a long pasted prompt doesn't
            # blow up the preview, but keep enough to identify which
            # turn it came from.
            short = trig if len(trig) <= 240 else trig[:237] + "…"
            head.append(short, style="#aaaaaa")
        title = f"running · {item.get('skill_name', '?')}"
        pane.show_text(title, RichGroup(head))

    def _preview_running_plan(self, pane: _PreviewPane, item: dict) -> None:
        """Live snapshot for an in-flight plan run."""
        from rich.console import Group as RichGroup
        from rich.text import Text as RichText
        head = RichText()
        head.append("plan ", style="dim")
        head.append(item.get("plan_id", "?"), style="bold #ff9944")
        head.append("  ")
        head.append(item.get("status", "?"),
                    style="#44cc88" if item.get("status") == "running" else "#aaaa55")
        head.append("\n")
        head.append("agent: ", style="dim")
        head.append(item.get("agent", "?"), style="#dddddd")
        head.append("\n")
        head.append("progress: ", style="dim")
        head.append(
            f"{item.get('done', 0)}/{item.get('total', 0)}", style="#dddddd",
        )
        if item.get("failed"):
            head.append(f"  ({item['failed']} failed)", style="#ff6644")
        if item.get("goal"):
            head.append("\n\ngoal:\n", style="dim")
            head.append(item["goal"], style="#aaaaaa")
        title = f"running · plan {item.get('plan_id', '?')}"
        pane.show_text(title, RichGroup(head))

    def _preview_recent_skill(self, pane: _PreviewPane, item: dict) -> None:
        """Replay events from the skill_run jsonl file."""
        import json as _json

        from rich.console import Group as RichGroup
        from rich.text import Text as RichText

        head = RichText()
        status = item.get("status", "?")
        if status == "ok":
            head_glyph, head_style = "✓ ", "#44cc88"
        elif status == "stuck":
            head_glyph, head_style = "⊘ ", "#ffaa44"
        else:
            head_glyph, head_style = "✗ ", "#ff6644"
        head.append(head_glyph, style="bold " + head_style)
        head.append(item.get("skill_name", "?"), style="bold #dddddd")
        head.append("\n")
        head.append("agent: ", style="dim")
        head.append(item.get("agent", "?"), style="#dddddd")
        head.append("\n")
        head.append("status: ", style="dim")
        if status == "stuck":
            head.append(
                f"stuck (last event: {item.get('stuck_at', '?')})",
                style="bold #ffaa44",
            )
            head.append(
                "\n           "
                "no workflow_finished / workflow_aborted in the log — "
                "the run was likely killed mid-execution "
                "(SIGKILL / crash / abandoned session)",
                style="dim #aa8844",
            )
        else:
            head.append(status, style=head_style)
        head.append("\n")
        head.append("duration: ", style="dim")
        head.append(f"{item.get('duration_s', 0):.1f}s", style="#dddddd")

        # Event log + LLM-usage rollup + phase-path reconstruction.
        # Single pass: keep running totals for LLM calls / tokens / cost
        # AND record the sequence of phase_started events so we can
        # render the actual execution path the OS took. The events list
        # is also retained for the YAML dump below, so we don't read
        # the file twice.
        events: list[dict] = []
        llm_calls = 0
        prompt_tokens_total = 0
        completion_tokens_total = 0
        cost_usd_total = 0.0
        phase_path: list[tuple[str, int]] = []   # [(phase_name, visit_count)]
        terminal_kind: str = ""                  # "finished" / "aborted" / ""
        path = item.get("jsonl_path")
        if path is not None:
            try:
                for raw in path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = _json.loads(raw)
                    except Exception:
                        continue
                    events.append(ev)
                    et = ev.get("type")
                    d = ev.get("data") or {}
                    if et == "llm_response_received":
                        llm_calls += 1
                        try:
                            prompt_tokens_total += int(d.get("prompt_tokens", 0) or 0)
                        except (TypeError, ValueError):
                            pass
                        try:
                            completion_tokens_total += int(
                                d.get("completion_tokens", 0) or 0
                            )
                        except (TypeError, ValueError):
                            pass
                        try:
                            cost_usd_total += float(d.get("cost_usd", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            pass
                    elif et == "phase_started":
                        phase_name = str(d.get("phase", "")).strip()
                        if phase_name:
                            try:
                                visit = int(d.get("visit_count", 1) or 1)
                            except (TypeError, ValueError):
                                visit = 1
                            phase_path.append((phase_name, visit))
                    elif et in ("workflow_finished", "skill_run_completed"):
                        terminal_kind = "finished"
                    elif et in ("workflow_aborted", "skill_run_failed"):
                        terminal_kind = "aborted"
            except OSError as exc:
                head.append(f"\n(failed to read events: {exc})", style="dim red")

        # LLM usage block — only meaningful when we actually saw at least
        # one llm_response_received event. Skipped silently for runs that
        # didn't call the LLM (e.g. pure deterministic preprocessor).
        if llm_calls > 0:
            head.append("\n")
            head.append("llm calls: ", style="dim")
            head.append(str(llm_calls), style="#dddddd")
            head.append("\n")
            head.append("tokens: ", style="dim")
            total_tokens = prompt_tokens_total + completion_tokens_total
            head.append(
                f"{total_tokens:,} (prompt {prompt_tokens_total:,} + "
                f"completion {completion_tokens_total:,})",
                style="#dddddd",
            )
            head.append("\n")
            head.append("cost: ", style="dim")
            head.append(f"${cost_usd_total:.4f}", style="#dddddd")

        # Phase graph — the sequence of phases the OS actually executed,
        # rendered with arrows. Visit counts > 1 surface as `(v2)` /
        # `(v3)` so revisit loops are visible. Long paths wrap on the
        # arrow boundary so a 10+ phase chain stays readable in a 33%
        # preview pane. The terminal type appends a final node:
        #   finished → arrow into a green ✓ end
        #   aborted  → arrow into a red ✗ aborted
        if phase_path:
            head.append("\n")
            head.append("phase graph:", style="dim")
            head.append("\n  ")
            line_chars = 2  # current visual column inside this line
            for i, (phase, visit) in enumerate(phase_path):
                node = phase if visit <= 1 else f"{phase} (v{visit})"
                # Soft-wrap before drawing the arrow when adding it would
                # push past ~58 chars (= comfortable width inside the
                # preview pane border).
                arrow = " → " if i > 0 else ""
                if i > 0 and line_chars + len(arrow) + len(node) > 58:
                    head.append("\n  ")
                    line_chars = 2
                if arrow:
                    head.append(arrow, style="dim #555555")
                    line_chars += len(arrow)
                head.append(node, style="#dddddd")
                line_chars += len(node)
            # Terminal marker
            if terminal_kind == "finished":
                tail_text = " → ✓ end"
                tail_style = "#44cc88"
            elif terminal_kind == "aborted":
                tail_text = " → ✗ aborted"
                tail_style = "#ff6644"
            else:
                tail_text = ""
                tail_style = ""
            if tail_text:
                if line_chars + len(tail_text) > 58:
                    head.append("\n  ")
                head.append(tail_text, style=tail_style)

        head.append("\n")
        head.append("finished: ", style="dim")
        head.append(item.get("ts", "?"), style="#dddddd")

        # triggered_by — looked up by full run_id from the session-local
        # map populated when the skill first emitted a trace event. Empty
        # for runs from previous sessions (= map is rebuilt fresh each
        # ``reyn chat`` launch).
        trig = self._lookup_triggered_by(item.get("run_id_full", ""))
        if trig:
            head.append("\n\ntriggered by:\n", style="dim")
            short = trig if len(trig) <= 240 else trig[:237] + "…"
            head.append(short, style="#aaaaaa")

        head.append("\n\n")

        if events:
            event_block = self._render_as_yaml(events)
            title = f"{item.get('skill_name', '?')} · {len(events)} events"
            pane.show_text(title, RichGroup(head, event_block))
        else:
            head.append("(no events found)", style="dim #555555")
            title = f"{item.get('skill_name', '?')} · 0 events"
            pane.show_text(title, RichGroup(head))

    # ── generic 'c'-to-copy plumbing ────────────────────────────────────────

    def _copy_current_view(self) -> None:
        """Copy whatever region of the right panel currently has focus.

        Focus-based routing — matches how the user reads the panel:

          tabs focused / _PanelTop focused → copy upper-panel content
                                              (= the active tab's main
                                              renderable, as plain text)
          _PreviewPane focused             → copy preview content
                                              (= the cursor's detail view)

        Falls back to upper-panel copy when focus is outside the panel
        entirely (= ``c`` was triggered programmatically or focus is on
        an unrelated widget). The user's mental model is "I'm looking
        at X → press c → X is in my clipboard"; focus is the most
        reliable signal of "what they're looking at" because preview
        can stay open while they're navigating tabs.

        Status confirmation flows through the conv pane's sticky-status
        widget — auto-clears after 2.5 s.
        """
        try:
            text, label = self._build_copy_payload()
        except Exception as exc:
            self._copy_status(f"✗ failed to build copy payload: {exc}", error=True)
            return
        if not text:
            self._copy_status(f"(nothing to copy from {label})", error=True)
            return
        from ..._clipboard import copy_to_clipboard
        ok, tool = copy_to_clipboard(text)
        if ok:
            self._copy_status(
                f"✓ copied {label} · {len(text):,} chars via {tool}",
            )
        else:
            self._copy_status(
                "✗ no clipboard tool found "
                "(install pbcopy / xclip / wl-copy / xsel)",
                error=True,
            )

    def _build_copy_payload(self) -> tuple[str | None, str]:
        """Return ``(text, label)`` for the focused right-panel region.

        Routes by *focus*, not by preview visibility — the user can keep
        the preview open while navigating tabs in the upper area, and
        the keystroke should target whichever region is "live" in their
        attention. Tabs / _PanelTop = main; _PreviewPane = preview;
        anything else = main (safest default — the upper area is where
        first-time users land).
        """
        if self._is_preview_focused():
            return self._build_preview_copy_text()
        return self._build_main_copy_text()

    def _is_preview_focused(self) -> bool:
        """True iff focus lives inside the preview pane right now."""
        focused = self.app.focused
        if focused is None:
            return False
        try:
            preview = self.query_one("#preview-pane", _PreviewPane)
        except Exception:
            return False
        if focused is preview:
            return True
        return any(a is preview for a in focused.ancestors)

    def _build_preview_copy_text(self) -> tuple[str | None, str]:
        """Plain-text version of the active preview content.

        Each tab routes to its own builder where there's a dedicated
        format that's nicer than the rendered Rich text — the agents
        bundles especially. Other tabs fall back to rendering the same
        Rich renderable as plain text.
        """
        if self._panel_type == "agents" and self._agents_items:
            idx = max(0, min(len(self._agents_items) - 1, self._agents_cursor))
            item = self._agents_items[idx]
            kind = item.get("kind", "")
            if kind == "recent_skill":
                return (
                    self._build_recent_skill_bundle(item),
                    f"skill run · {item.get('skill_name', '?')}",
                )
            if kind == "recent_plan":
                return (
                    self._build_recent_plan_bundle(item),
                    f"plan · {item.get('plan_id', '?')}",
                )
            if kind == "running_skill":
                return (
                    self._build_running_skill_bundle(item),
                    f"running skill · {item.get('skill_name', '?')}",
                )
            if kind == "running_plan":
                return (
                    self._build_running_plan_bundle(item),
                    f"running plan · {item.get('plan_id', '?')}",
                )
        if self._panel_type == "docs" and self._docs_files:
            idx = max(0, min(len(self._docs_files) - 1, self._docs_cursor))
            doc_path: Path = self._docs_files[idx]
            try:
                body = doc_path.read_text(encoding="utf-8")
            except OSError as exc:
                return (None, f"docs preview ({exc})")
            header = f"# {doc_path.name}\n# source: {doc_path}\n\n"
            return (header + body, f"doc · {doc_path.name}")
        # Generic fallback: capture whatever the preview builder hands
        # to the pane and render it to plain text.
        text = self._capture_preview_as_text()
        if text:
            return (text, f"{self._panel_type} preview")
        return (None, f"{self._panel_type} preview")

    def _build_main_copy_text(self) -> tuple[str | None, str]:
        """Plain-text snapshot of the active tab's main content."""
        try:
            renderable = self._panel_markup()
        except Exception as exc:
            return (None, f"{self._panel_type} view ({exc})")
        text = self._renderable_to_text(renderable)
        return (text, f"{self._panel_type} view")

    def _capture_preview_as_text(self) -> str | None:
        """Re-run the preview builder and snapshot its renderable as text.

        Mirrors the dispatch in ``_update_preview`` but writes into a
        capture stub instead of the real preview pane, so we can grab
        the title + renderable and convert to plain text.
        """
        captured: list[tuple[str, Any]] = []

        class _CapturePane:
            def show_text(self, title: str, renderable: Any) -> None:
                captured.append((title, renderable))

            def clear(self) -> None:
                captured.append(("", None))

        fake = _CapturePane()
        try:
            if self._panel_type == "events" and self._events_visible:
                self._show_event_in_preview(fake)  # type: ignore[arg-type]
            elif self._panel_type == "memory" and self._memory_entries:
                self._show_memory_in_preview(fake)  # type: ignore[arg-type]
            else:
                return None
        except Exception as exc:
            logger.warning("right_panel capture preview failed: %s", exc)
            return None
        if not captured:
            return None
        title, renderable = captured[-1]
        if renderable is None:
            return None
        body = self._renderable_to_text(renderable)
        return f"# {title}\n\n{body}" if title else body

    def _lookup_triggered_by(self, run_id_full: str) -> str:
        """Return the user message that triggered ``run_id_full`` (or "")."""
        if not run_id_full:
            return ""
        try:
            return str(
                self.app._run_id_to_user_message.get(run_id_full, ""),  # type: ignore[attr-defined]
            )
        except Exception:
            return ""

    def _renderable_to_text(self, renderable: Any) -> str:
        """Render any Rich renderable to plain text (no ANSI codes)."""
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(
            file=buf,
            width=120,
            no_color=True,
            force_terminal=False,
            legacy_windows=False,
            record=False,
        ).print(renderable)
        return buf.getvalue()

    def _copy_status(self, text: str, *, error: bool = False) -> None:
        """Write a brief copy-action status line to the conv pane (auto-clears)."""
        from .. import ConversationView  # late import → avoid cycle
        try:
            conv = self.app.query_one("#conversation", ConversationView)
        except Exception:
            return
        try:
            conv.show_status(text, kind="error" if error else "general")
            self.app.set_timer(2.5, conv.hide_status)
        except Exception as exc:
            logger.warning("right_panel copy status failed: %s", exc)

    def _prefill_docs_filter(self) -> None:
        """`/` on the Docs tab — focus InputBar and pre-fill `/docs-filter `.

        MVP: hand off to the existing `/docs-filter` slash command instead of
        building an inline filter buffer. The user types the substring and
        hits Enter; an empty submission clears the filter (same as invoking
        the slash command directly).
        """
        from .. import InputBar  # late import → avoid cycle
        try:
            inputbar = self.app.query_one("#inputbar", InputBar)
        except Exception as exc:
            logger.warning("right_panel prefill docs filter failed: %s", exc)
            return
        try:
            ta = inputbar.query_one("#input")
            ta.load_text("/docs-filter ")
            # Move cursor past the trailing space so the user types the
            # substring directly without having to skip whitespace.
            ta.move_cursor((0, len("/docs-filter ")))
            inputbar.focus_input()
        except Exception as exc:
            logger.warning("right_panel prefill docs filter focus failed: %s", exc)

    def _build_recent_skill_bundle(self, item: dict) -> str:
        """Header + events YAML for a finished skill run."""
        import json as _json
        try:
            import yaml as _yaml
        except Exception:
            _yaml = None  # type: ignore[assignment]

        events: list[dict] = []
        llm_calls = 0
        ptok = 0
        ctok = 0
        cost = 0.0
        phase_path: list[tuple[str, int]] = []
        terminal_kind = ""
        path = item.get("jsonl_path")
        if path is not None:
            try:
                for raw in path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = _json.loads(raw)
                    except Exception:
                        continue
                    events.append(ev)
                    et = ev.get("type")
                    d = ev.get("data") or {}
                    if et == "llm_response_received":
                        llm_calls += 1
                        try:
                            ptok += int(d.get("prompt_tokens", 0) or 0)
                            ctok += int(d.get("completion_tokens", 0) or 0)
                            cost += float(d.get("cost_usd", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            pass
                    elif et == "phase_started":
                        phase_name = str(d.get("phase", "")).strip()
                        if phase_name:
                            try:
                                visit = int(d.get("visit_count", 1) or 1)
                            except (TypeError, ValueError):
                                visit = 1
                            phase_path.append((phase_name, visit))
                    elif et in ("workflow_finished", "skill_run_completed"):
                        terminal_kind = "finished"
                    elif et in ("workflow_aborted", "skill_run_failed"):
                        terminal_kind = "aborted"
            except OSError:
                pass

        lines: list[str] = []
        lines.append(f"# Reyn skill run · {item.get('skill_name', '?')}")
        lines.append(f"# agent:    {item.get('agent', '?')}")
        lines.append(f"# run_id:   {item.get('run_id', '?')}")
        status = item.get("status", "?")
        if status == "stuck":
            lines.append(
                f"# status:   stuck (last event: {item.get('stuck_at', '?')}) "
                f"-- run was killed mid-execution; no terminal event"
            )
        else:
            lines.append(f"# status:   {status}")
        lines.append(f"# duration: {item.get('duration_s', 0):.1f}s")
        if llm_calls > 0:
            lines.append(f"# llm:      {llm_calls} call(s)")
            lines.append(
                f"# tokens:   {ptok + ctok:,} (prompt {ptok:,} + completion {ctok:,})"
            )
            lines.append(f"# cost:     ${cost:.4f}")
        if phase_path:
            chain_parts = [
                f"{p} (v{v})" if v > 1 else p for p, v in phase_path
            ]
            chain = " → ".join(chain_parts)
            if terminal_kind == "finished":
                chain += " → ✓ end"
            elif terminal_kind == "aborted":
                chain += " → ✗ aborted"
            lines.append(f"# graph:    {chain}")
        lines.append(f"# finished: {item.get('ts', '?')}")
        if path is not None:
            lines.append(f"# source:   {path}")
        trig = self._lookup_triggered_by(item.get("run_id_full", ""))
        if trig:
            lines.append("")
            lines.append("triggered_by: |")
            for ln in str(trig).splitlines() or [str(trig)]:
                lines.append(f"  {ln}")
        lines.append("")
        if events:
            if _yaml is not None:
                try:
                    normalised = _json.loads(
                        _json.dumps(events, default=str, ensure_ascii=False),
                    )
                    body = _yaml.safe_dump(
                        {"events": normalised},
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                        width=120,
                    )
                    lines.append(body.rstrip())
                except Exception:
                    lines.append(_json.dumps(events, indent=2, default=str))
            else:
                lines.append(_json.dumps(events, indent=2, default=str))
        else:
            lines.append("(no events)")
        return "\n".join(lines) + "\n"

    def _build_recent_plan_bundle(self, item: dict) -> str:
        """Header for a finished plan."""
        lines: list[str] = []
        lines.append(f"# Reyn plan · {item.get('plan_id', '?')}")
        lines.append(f"# agent:    {item.get('agent', '?')}")
        lines.append(f"# status:   {item.get('status', '?')}")
        lines.append(
            f"# steps:    {item.get('n_completed', 0)} ok / "
            f"{item.get('n_failed', 0)} failed",
        )
        if item.get("exc_type"):
            lines.append(f"# exc:      {item['exc_type']}")
        lines.append(f"# finished: {item.get('ts', '?')}")
        if item.get("goal"):
            lines.append("")
            lines.append("goal: |")
            for goal_line in str(item["goal"]).splitlines() or [str(item["goal"])]:
                lines.append(f"  {goal_line}")
        return "\n".join(lines) + "\n"

    def _build_running_skill_bundle(self, item: dict) -> str:
        lines: list[str] = []
        lines.append(f"# Reyn skill run (running) · {item.get('skill_name', '?')}")
        lines.append(f"# agent:    {item.get('agent', '?')}")
        lines.append(f"# run_id:   {item.get('run_id', '?')}")
        lines.append(f"# elapsed:  {item.get('elapsed_s', 0)}s")
        lines.append(f"# phase:    {item.get('phase', '—') or '—'}")
        if item.get("phase_visits", 0) > 1:
            lines.append(f"# visits:   {item['phase_visits']}")
        if item.get("triggered_by"):
            lines.append("")
            lines.append("triggered_by: |")
            for ln in str(item["triggered_by"]).splitlines() or [str(item["triggered_by"])]:
                lines.append(f"  {ln}")
        return "\n".join(lines) + "\n"

    def _build_running_plan_bundle(self, item: dict) -> str:
        lines: list[str] = []
        lines.append(f"# Reyn plan (running) · {item.get('plan_id', '?')}")
        lines.append(f"# agent:    {item.get('agent', '?')}")
        lines.append(f"# status:   {item.get('status', '?')}")
        lines.append(
            f"# progress: {item.get('done', 0)}/{item.get('total', 0)} "
            f"({item.get('failed', 0)} failed)"
        )
        if item.get("goal"):
            lines.append("")
            lines.append("goal: |")
            for ln in str(item["goal"]).splitlines() or [str(item["goal"])]:
                lines.append(f"  {ln}")
        return "\n".join(lines) + "\n"

    def _preview_recent_plan(self, pane: _PreviewPane, item: dict) -> None:
        """Detail view for a finished plan."""
        from rich.console import Group as RichGroup
        from rich.text import Text as RichText
        head = RichText()
        ok = item.get("status") == "ok"
        head.append("✓ " if ok else "✗ ",
                    style="bold " + ("#44cc88" if ok else "#ff6644"))
        head.append("plan ", style="dim")
        head.append(item.get("plan_id", "?"), style="bold #ff9944")
        head.append("\n")
        head.append("agent: ", style="dim")
        head.append(item.get("agent", "?"), style="#dddddd")
        head.append("\n")
        head.append("status: ", style="dim")
        head.append(item.get("status", "?"),
                    style="#44cc88" if ok else "#ff6644")
        head.append("\n")
        head.append("steps: ", style="dim")
        head.append(
            f"{item.get('n_completed', 0)} ok / {item.get('n_failed', 0)} failed",
            style="#dddddd",
        )
        head.append("\n")
        head.append("finished: ", style="dim")
        head.append(item.get("ts", "?"), style="#dddddd")
        if item.get("exc_type"):
            head.append("\n\nexception:\n", style="dim")
            head.append(item["exc_type"], style="#ff6644")
        if item.get("goal"):
            head.append("\n\ngoal:\n", style="dim")
            head.append(item["goal"], style="#aaaaaa")
        title = f"plan {item.get('plan_id', '?')}"
        pane.show_text(title, RichGroup(head))

    # ── docs navigation ───────────────────────────────────────────────────────

    def _docs_move(self, delta: int) -> None:
        """Move the docs cursor over the flat file list; sync preview / scroll.

        Cursor wraps around modulo the file count (Vim-list behaviour).
        Wrapping is over the flat `_docs_files` order — sections are a
        rendering concern, not a separate navigation axis here.
        """
        n = len(self._docs_files)
        if n == 0:
            self._docs_cursor = 0
            return
        self._docs_cursor = (self._docs_cursor + delta) % n
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
            return f"[bold {_CORAL}]Key Bindings[/]  [#555555]j↓ k↑[/]"
        if self._panel_type == "agents":
            return f"[bold {_CORAL}]Agents[/]  [#555555]j↓ k↑[/]"
        if self._panel_type == "memory":
            return f"[bold {_CORAL}]Memory[/]  [#555555]j↓ k↑ space=open[/]"
        if self._panel_type == "cost":
            return f"[bold {_CORAL}]Cost[/]  [#555555]j↓ k↑[/]"
        if self._panel_type == "docs":
            return (
                f"[bold {_CORAL}]Docs[/]"
                f"  [#555555]j↓ k↑ space=open /=filter[/]"
            )
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
                    filelist_cache=self._events_filelist_cache,
                )
                self._events_visible = windowed
                if self._events_cursor >= len(windowed):
                    self._events_cursor = max(0, len(windowed) - 1)
                return rendered
            if self._panel_type == "agents":
                rendered, flat_items = render_agents(
                    self._registry, self._exec_state,
                    project_root=self._project_root,
                    cursor=self._agents_cursor,
                )
                self._agents_items = flat_items
                if self._agents_cursor >= len(flat_items):
                    self._agents_cursor = max(0, len(flat_items) - 1)
                return rendered
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
