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
from .keys_tab import (
    get_keys_cursor,
    get_keys_expanded,
    keys_move,
    render_keys,
    toggle_expand_cursor,
)
from .memory_tab import render_memory
from .pending_tab import render_pending
from .shells import (
    _PanelContent,
    _PanelHeader,
    _PanelTop,
    _PreviewPane,
    _TabContent,
)

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry


PANEL_TYPES: list[str] = [
    "keys", "events", "agents", "memory", "cost", "docs", "pending",
]

_PANEL_LABELS: dict[str, str] = {
    "keys":    "Keys",
    "events":  "Events",
    "agents":  "Agents",
    "memory":  "Memory",
    "cost":    "Cost",
    "docs":    "Docs",
    # Issue #277: stalled / cross-channel pending operations surface
    # (= #270 First instance = Phase A TUI surface split from #268).
    "pending": "Pending",
}

# Tabs that re-render on the periodic ``_REFRESH_INTERVAL`` tick so newly
# arrived data shows up without the user having to switch tabs away and
# back. Memory was missing — when reyn saved or deleted an entry mid-
# session it stayed invisible in the Memory tab until next refresh
# trigger (= manual tab switch).
_LIVE_PANELS = {"events", "agents", "cost", "memory", "pending"}
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
        min-width: 44;
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
        # Language preference for the Docs tab. Two valid values: "en"
        # (default — English preferred, Japanese fallback when .md absent)
        # and "ja" (Japanese preferred, English fallback). Toggled by the
        # ``g`` key while the Docs tab is active.
        self._docs_lang: str = "en"
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
        # y-coord (0-indexed line) of each event's headline row in the
        # rendered output. Populated by `render_events`; used by
        # `_scroll_events_into_view` so chain-switch blank lines + the
        # extra ``↳`` reply line on ``user_message_received`` rows don't
        # drift the viewport off the cursor (A-F3, wave-8). Same idiom
        # as ``_memory_entry_ys`` / ``_agents_item_ys``.
        self._events_event_ys: list[int] = []
        # Chain isolation — when set to a chain_id string, ``render_events``
        # restricts the visible list to events sharing that chain_id (=
        # "show me only this one conversation thread"). Toggled by the
        # ``i`` key on the events tab: first press captures the cursor's
        # chain_id; second press clears. Wave-11 A#2 — interleaved-chain
        # tail at tail=200 is unreadable; isolate makes one chain
        # legible at a time.
        self._events_chain_isolate: str | None = None
        # Verbose mode — when False (default), ``render_events`` hides
        # ``compaction_check`` events which fire on every chat turn but
        # mostly carry "didn't compact" outcomes (too_few_turns /
        # below_min_batch / below_threshold / already_running). The actual
        # compaction lifecycle (compaction_started / completed / failed)
        # is always shown. Toggled by ``v`` on the events tab.
        self._events_verbose: bool = False
        # Memory tab state — flat cursor over all entries
        self._memory_cursor: int = 0
        self._memory_entries: list[Any] = []
        # Wave-11 A#1 — memory tab per-type filter. Cycled by the ``t``
        # key on memory tab (events tab's ``t`` is gated to events-only
        # via check_action, so the two share the key without conflict).
        # Cycle: None → "user" → "feedback" → "project" → "reference"
        # → None. Pinned at None on cold-default so the full list is
        # the starting view.
        self._memory_type_filter: str | None = None
        # Latest ARS hot-list ranking from ChatLifecycleForwarder (issue
        # #192). ``[{"qualified_name": str, "freq": int, "last_ts": str},
        # ...]`` full ranking. Fed by ``update_hot_list`` (= driven from
        # ``OutboxRouter._on_hot_list_updated``). The Memory tab renders
        # a "Hot now" sub-section above SHARED / AGENT scopes when the
        # list is non-empty; otherwise the section is omitted entirely
        # so the existing layout is unchanged on cold-start.
        self._hot_list_ranking: list[dict] = []
        # y-coord (0-indexed line) of each memory entry's name row in the
        # rendered output. Populated by `render_memory`; used by
        # `_scroll_memory_into_view` to keep the cursor visible as j/k
        # moves it past the panel viewport.
        self._memory_entry_ys: list[int] = []
        # Agents tab state — same cursor pattern as events / memory.
        # `_agents_items` is the flat list returned by `render_agents`,
        # one entry per running skill / running plan / recent skill /
        # recent plan. Used by j/k navigation and preview rendering.
        self._agents_cursor: int = 0
        self._agents_items: list[dict] = []
        # y-coord of each agents-tab item; populated by `render_agents`.
        self._agents_item_ys: list[int] = []
        # Pending tab state (issue #277) — j/k cursor over stalled
        # PendingOpView items. ``_pending_items`` is the flat list
        # returned by ``render_pending`` (= one dict per stalled op).
        # ``_pending_item_ys`` records the y of each item's primary
        # row in the rendered output (= for scroll-into-view).
        self._pending_cursor: int = 0
        self._pending_items: list[dict] = []
        self._pending_item_ys: list[int] = []
        # y-coord (0-indexed line) of each key row in the rendered output.
        # Populated by ``render_keys``; used by ``_scroll_keys_into_view``
        # so j/k navigation keeps the cursor row visible within #panel-scroll.
        self._key_ys: list[int] = []

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

    def on_resize(self, event) -> None:
        """Re-clamp ``_panel_width`` when the terminal window resizes.

        Without this, ``_panel_width`` (an absolute column count cached
        on h / l) survives a terminal shrink unchanged — a 145-col
        panel on a 220-col terminal stayed at 145 cols after a
        ``tmux resize-window -x 80``, overflowing the new 80-col
        window and crushing the conv pane to ~0 cols. The user
        couldn't see chat output until they reopened the panel
        manually.

        Clamp against the freshly-computed max (= 66 % of the new
        terminal width, floor 40 cols) so the panel always stays
        inside the terminal. Skip the work when the panel hasn't been
        resized yet (= ``_panel_width == 0`` means it's still using
        the CSS default of 33 % and Textual will reflow it
        automatically).
        """
        del event  # unused; we re-read app.size below
        if self._panel_width == 0:
            return
        max_width = self._max_panel_width()
        if self._panel_width > max_width:
            self._panel_width = max_width
            try:
                self.styles.width = self._panel_width
            except Exception as exc:
                logger.warning(
                    "right_panel on_resize re-clamp failed: %s", exc,
                )

    def _max_panel_width(self) -> int:
        """Return the upper bound for ``_panel_width`` against the current terminal.

        66 % of terminal width, floor 40 cols (= same formula
        ``_panel_resize`` uses). Centralised so the resize handler and
        the manual resize path can't drift apart.
        """
        return max(40, int((self.app.size.width or 120) * 0.66))

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

        Wave-10 follow-up H-F3: when the preview pane is open on a
        running-skill / running-plan row, refresh it too. Pre-fix the
        main tree updated correctly (= ``_invalidate`` → ``_panel_markup``
        → ``render_agents`` rebuilt ``_agents_items`` in place), but the
        preview pane snapshot was frozen to whatever
        ``_show_*_in_preview`` had last captured (= last navigation /
        Space toggle). A user watching a long skill saw the main tree
        tick ``elapsed: 104s`` while the preview still read
        ``elapsed: 42s`` from when they opened it — undermining the
        preview's role as "the detail view of the live run". Other
        live-tick consumers (no other tab uses ``_exec_state``) are
        unaffected; the guard scopes the refresh to ``agents`` only.
        """
        self._exec_state = dict(state)
        if self._panel_type == "agents":
            self._invalidate()
            if self._preview_visible:
                self._update_preview()

    def update_hot_list(self, ranking: list[dict]) -> None:
        """Receive the latest ARS hot-list ranking from the TUI app.

        Called from ``OutboxRouter._on_hot_list_updated`` whenever the
        forwarder emits a ``hot_list_updated`` outbox message (= the
        qualified-name order changed). Triggers a re-render only when
        the Memory tab is visible — the cached ranking is consumed on
        the next paint of any other tab via the same field.
        """
        self._hot_list_ranking = list(ranking)
        if self._panel_type == "memory":
            self._invalidate()

    def _panel_resize(self, delta: int) -> None:
        if self._panel_width == 0:
            self._panel_width = self.size.width or 40
        max_width = self._max_panel_width()
        # Min width is sized so the full 7-tab bar
        # (Keys/Events/Agents/Memory/Cost/Docs/Pending ≈ 44 cells incl.
        # margins) always fits. Below this the Textual Tabs widget
        # silently scrolls the right-most tabs out of view with no
        # overflow marker, leaving the user no way to tell which tabs
        # exist by glancing at the bar — Ctrl+W still cycles them but
        # the visible set is a misleading subset.
        #
        # Bump history:
        # - 24 → 36 (6 tabs era: Keys/Events/Agents/Memory/Cost/Docs)
        # - 36 → 44 (= wave-3 SP1, 7-tab era: + Pending from issue #277)
        #   At 36 cells, "Pending" truncated to "Pend…" or scrolled
        #   out entirely at the default 33%-width panel; bumping to 44
        #   guarantees the full label fits with the new tab.
        _MIN_WIDTH = 44
        new_width = max(_MIN_WIDTH, min(max_width, self._panel_width + delta))
        # Flash the new width in the conv sticky bar so the user sees
        # something changed (and learns the bounds via the at-min /
        # at-max suffix). Skip the flash on no-op clamps to avoid the
        # impression that the keystroke was lost when nothing moved.
        at_min = new_width == _MIN_WIDTH
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
        # Re-anchor the cursor in the viewport — the new filter usually
        # changes the list length and clamps the cursor to a different
        # row, so without this the cursor was clipped out of view until
        # the user pressed j/k.
        self._scroll_events_into_view()

    def cycle_event_tail(self) -> None:
        """Rotate through tail-N values; only meaningful on events tab."""
        self._event_tail_idx = (self._event_tail_idx + 1) % len(_TAIL_CYCLE)
        self._invalidate()
        # Wave-11 A#4 — re-anchor the cursor in the viewport. Mirrors
        # the ``cycle_event_filter`` idiom above. Without this, cycling
        # 30→200 with cursor near the bottom of the old window left
        # the cursor invisible until the next j/k press because the
        # viewport stayed pinned at the old scroll position. Asymmetric
        # absence; same root cause as #588's chain-isolate scroll
        # restoration.
        self._scroll_events_into_view()

    def cycle_memory_type_filter(self) -> str | None:
        """Cycle the memory tab's type-filter through the known kinds.

        Order: ``None`` → ``"user"`` → ``"feedback"`` → ``"project"``
        → ``"reference"`` → ``None``. Returns the new value. Resets
        the memory cursor to 0 because the visible-list shape just
        changed and clamping the old index would land arbitrarily.
        """
        _cycle = (None, "user", "feedback", "project", "reference")
        try:
            current_idx = _cycle.index(self._memory_type_filter)
        except ValueError:
            current_idx = 0  # garbage state → start fresh
        new_filter = _cycle[(current_idx + 1) % len(_cycle)]
        self._memory_type_filter = new_filter
        self._memory_cursor = 0
        self._invalidate()
        return new_filter

    def toggle_chain_isolate(self) -> bool:
        """Toggle events-tab chain isolation centered on the cursor.

        When isolation is OFF, captures the chain_id of the event under
        the cursor and switches the visible list to that chain only.
        When isolation is ON, clears it (= back to the full filtered
        tail). Returns True if isolation became active, False if cleared
        or no-op (= cursor outside list, or cursor event has no
        chain_id).
        """
        if self._events_chain_isolate is not None:
            # Clear isolation.
            self._events_chain_isolate = None
            self._invalidate()
            self._scroll_events_into_view()
            return False
        # Capture cursor's chain_id.
        if not self._events_visible:
            return False
        idx = max(0, min(len(self._events_visible) - 1, self._events_cursor))
        ev = self._events_visible[idx]
        chain_id = (ev.get("data") or {}).get("chain_id") or ""
        if not chain_id:
            # Cursor event has no chain_id (= bare system event). Don't
            # silently set an empty-string isolate; let caller surface a
            # hint instead.
            return False
        self._events_chain_isolate = chain_id
        # Reset cursor to top of the filtered list (= the newest event
        # in this chain). The filtered list ordering is preserved by
        # render_events.
        self._events_cursor = 0
        self._invalidate()
        self._scroll_events_into_view()
        return True

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
        elif event.key == "escape":
            # Esc inside the panel returns focus to the input bar — the
            # standard "back out of a sub-context" affordance. Without
            # this, the panel was a Tab/Shift+Tab focus trap (those keys
            # cycle tabs, never exit), and Esc was a silent no-op because
            # the App's escape binding is gated by ``check_action`` which
            # returns False when nothing else (recording, error box) is
            # interceptable. Tab cycling stays; this is purely the exit.
            # A-F4 (wave-8): when the docs tab has an active filter,
            # Esc clears the filter in place instead of dropping focus
            # back to the input bar. Without this, the only clear path
            # was running ``/docs-filter`` (with no argument) — a
            # mechanical 4-step (=  press `/`, delete pre-fill,
            # submit empty). Esc-to-clear matches the standard
            # "back out one level" affordance and keeps focus where
            # the user just was.
            event.prevent_default()
            event.stop()
            if self._panel_type == "docs" and self._docs_filter:
                self._docs_filter = ""
                self._invalidate()
                return
            from .. import InputBar  # late import → avoid cycle
            try:
                self.app.query_one("#inputbar", InputBar).focus_input()
            except Exception:
                pass
        elif event.key == "space":
            event.prevent_default()
            if self._panel_type == "keys":
                # Keys tab: Space toggles inline detail block for cursor row
                # (T1-4, Wave-12). Delegates to keys_tab module-level state
                # so the detail dict and cursor live close to the render logic.
                # Other tabs' Space semantics (= preview pane toggle) are
                # unchanged — gated by this branch.
                markup, flat_key_list, _key_ys = render_keys(
                    self.app,
                    cursor=get_keys_cursor(),
                    expanded=get_keys_expanded(),
                )
                toggle_expand_cursor(flat_key_list)
                self._invalidate()
            else:
                # Space is a uniform preview toggle: works on any tab when
                # focus is anywhere inside the right panel (tabs included).
                # _update_preview() dispatches per-tab content (docs/events/
                # memory); other tabs render a cleared preview pane.
                self._toggle_preview()
                if self._preview_visible:
                    self._update_preview()
        elif event.key == "j":
            event.prevent_default()
            if self._panel_type == "keys":
                self._keys_move(+1)
            elif self._panel_type == "docs":
                self._docs_move(+1)
            elif self._panel_type == "events":
                self._events_move(+1)
            elif self._panel_type == "memory":
                self._memory_move(+1)
            elif self._panel_type == "agents":
                self._agents_move(+1)
            elif self._panel_type == "pending":
                self._pending_move(+1)
            else:
                self._scroll_panel(+1)
        elif event.key == "k":
            event.prevent_default()
            if self._panel_type == "keys":
                self._keys_move(-1)
            elif self._panel_type == "docs":
                self._docs_move(-1)
            elif self._panel_type == "events":
                self._events_move(-1)
            elif self._panel_type == "memory":
                self._memory_move(-1)
            elif self._panel_type == "agents":
                self._agents_move(-1)
            elif self._panel_type == "pending":
                self._pending_move(-1)
            else:
                self._scroll_panel(-1)
        elif event.key == "t" and self._panel_type == "memory":
            # Wave-11 A#1 — memory tab ``t`` cycles per-type filter.
            # Events tab's ``t`` (= cycle_event_tail) is gated to
            # events-only via App.check_action, so the same key here
            # is contention-free. Picker for filter values cycles in
            # the order: None → user → feedback → project → reference.
            event.prevent_default()
            new_filter = self.cycle_memory_type_filter()
            label = new_filter.upper() if new_filter else "ALL"
            self._flash_status(f"memory filter: {label}")
        elif event.key == "i" and self._panel_type == "events":
            # Wave-11 A#2 — Events tab ``i`` = isolate cursor's chain_id.
            # First press: filter list to that chain only. Re-press:
            # clear isolation. Status hint when cursor's event has no
            # chain_id (= bare system event, can't isolate to nothing).
            event.prevent_default()
            became_active = self.toggle_chain_isolate()
            if (
                self._events_chain_isolate is None
                and not became_active
                and self._events_visible
            ):
                # Wasn't already-isolated AND toggle returned False →
                # cursor event has no chain_id. Surface a hint.
                self._flash_status("chain_id missing; cannot isolate")
        elif event.key == "f" and self._panel_type != "events":
            # T2-3 (Wave-12 B#5): ``f`` is only meaningful on the Events tab.
            # When the panel is on another tab, the app-level ``event_filter_cycle``
            # action silently no-ops (check_action returns False). Surface a
            # flash hint so the silent no-op stops being mysterious.
            event.prevent_default()
            self._flash_status("'f' only active on the Events tab")
        elif event.key == "t" and self._panel_type not in ("events", "memory"):
            # T2-3 (Wave-12 B#5): ``t`` is tab-gated (events tab → tail cycle;
            # memory tab → type filter). On any other tab it is silent no-op;
            # flash a hint so the user knows what tab to switch to.
            event.prevent_default()
            self._flash_status("'t' active on Events tab or Memory tab")
        elif event.key == "i" and self._panel_type != "events":
            # T2-3 (Wave-12 B#5): ``i`` is only meaningful on the Events tab.
            # The app-level binding doesn't gate this — RightPanel.on_key
            # owns it exclusively and only matches events panel above. Surface
            # a flash for the wrong-tab case.
            event.prevent_default()
            self._flash_status("'i' only active on the Events tab")
        elif event.key == "v" and self._panel_type == "events":
            # Events tab ``v`` = toggle verbose mode. When off (default),
            # compaction_check events are hidden (= "didn't compact" noise).
            # When on, the full unfiltered list is shown including
            # compaction_check. Scoped to events tab only — other tabs
            # don't consume ``v``.
            event.prevent_default()
            self._events_verbose = not self._events_verbose
            self._invalidate()
            state = "on" if self._events_verbose else "off"
            self._flash_status(f"events: verbose={state}")
        elif event.key == "v" and self._panel_type != "events":
            # ``v`` is only meaningful on the Events tab. Surface a flash
            # hint so a wrong-tab press isn't a silent no-op.
            event.prevent_default()
            self._flash_status("'v' only active on the Events tab")
        elif event.key == "a" and self._panel_type == "agents":
            # Wave-10 follow-up H-F11: Agents tab `a` = switch to the
            # cursor's agent. Mirrors the per-tab action idiom landed
            # by the pending tab's ``d`` / ``c`` keys: a single letter
            # owned by the tab whose semantics make sense only there.
            # MVP: prefill ``/attach <name>`` into the InputBar (same
            # handoff pattern as Docs tab ``/`` → ``/docs-filter``);
            # the user confirms with Enter, the existing slash command
            # dispatches. Direct ``session.attach`` calls bypass the
            # OS-level event log + permission checks the slash route
            # already gates through, so the prefill path is the
            # correct architectural seam.
            event.prevent_default()
            self._prefill_attach_for_cursor()
        elif event.key == "d" and self._panel_type == "events":
            # T2-5a (Wave-12): Events tab [d] → jump Docs tab to runtime/events.md.
            # Scoped to events tab only so the pending tab's `d` = discard and
            # any future per-tab `d` bindings remain unaffected.
            event.prevent_default()
            self._jump_docs_to_events_md()
        elif event.key == "d" and self._panel_type == "pending":
            # Issue #277 — Pending tab `d` = discard the cursor's iv.
            event.prevent_default()
            self._pending_action_discard()
        elif event.key == "c" and self._panel_type == "pending":
            # Issue #277 — Pending tab `c` = claim the cursor's iv to
            # the local channel. Note: this *overrides* the generic
            # ``c`` copy on Pending tab, since claim is the load-bearing
            # action there. Copy can be done via ``/copy`` slash in
            # other tabs.
            event.prevent_default()
            self._pending_action_claim()
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
        elif event.key == "g":
            # Docs tab only: toggle language preference between "ja"
            # (Japanese preferred, English fallback) and "en" (English
            # preferred, Japanese fallback). Each concept appears once
            # in the list; ``g`` is free on all other tabs (``ctrl+g``
            # is already used for find-next but bare ``g`` is unused).
            if self._panel_type == "docs":
                event.prevent_default()
                self._docs_lang = "en" if self._docs_lang == "ja" else "ja"
                other = "en" if self._docs_lang == "ja" else "ja"
                self._flash_status(
                    f"docs lang: {self._docs_lang} ({other} fallback)"
                )
                self._invalidate()


    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab and event.tab.id in PANEL_TYPES:
            self._panel_type = event.tab.id
            self._invalidate()
            # ``#panel-scroll`` is shared across tabs — when we switch to a
            # cursor-bearing tab whose cursor sits below the viewport
            # currently displayed (= a different tab's scroll position),
            # the cursor row goes invisible until the user presses j/k.
            # Re-anchor the viewport on the new tab's cursor so the ``▶``
            # marker is visible immediately.
            scroll_helper = {
                "events":  self._scroll_events_into_view,
                "agents":  self._scroll_agents_into_view,
                "memory":  self._scroll_memory_into_view,
                "docs":    self._scroll_docs_into_view,
                "pending": self._scroll_pending_into_view,
                "keys":    self._scroll_keys_into_view,
            }.get(self._panel_type)
            if scroll_helper is not None:
                scroll_helper()
            if self._preview_visible:
                # If the new tab has nothing previewable (= keys / cost,
                # or an empty memory / events list), auto-close the
                # preview so the user isn't left with a blank half-pane
                # and a `_toggle_preview` guard that silently no-ops on
                # the next Space press. Tabs that do have content keep
                # the preview open and refresh it.
                if not self._has_previewable_content():
                    self._toggle_preview()
                else:
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
        """Tick every ``_REFRESH_INTERVAL`` s; invalidate live tabs in view.

        Gated by ``self.display`` so a hidden right panel doesn't pay the
        refresh cost. The events tab's ``render_events`` walks every
        ``.reyn/events/*.jsonl`` to compute mtime / parse caches; on a
        long-running session that adds up to hundreds of ``stat()``
        syscalls every 2 s even when the user can't see the panel.
        Re-checking ``self.display`` is the cheapest filter — Textual's
        ``display`` reactive flips to ``False`` while ``Ctrl+B`` has the
        panel collapsed, so the gate is exact rather than heuristic.
        """
        if not self.display:
            return
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
        # Refuse to open the preview pane on a tab that has nothing to show
        # (e.g. agents tab with no running / recent skills, or memory tab
        # with an empty SHARED bucket). Otherwise the user sees a blank
        # `—` preview window with no obvious recovery, and has to press
        # Space again to dismiss it.
        if not self._preview_visible and not self._has_previewable_content():
            return
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

    def _has_previewable_content(self) -> bool:
        """Whether the current tab has at least one item the preview pane
        can render. Keys / cost tabs never have previews."""
        if self._panel_type == "docs":
            return bool(self._docs_files)
        if self._panel_type == "events":
            return bool(self._events_visible)
        if self._panel_type == "memory":
            return bool(self._memory_entries)
        if self._panel_type == "agents":
            return bool(self._agents_items)
        # A-F1 (wave-8): pending tab now has preview pane integration
        # so the user can see the full intervention detail without
        # claiming first.
        if self._panel_type == "pending":
            return bool(self._pending_items)
        return False

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
            elif self._panel_type == "pending" and self._pending_items:
                self._show_pending_in_preview(pane)
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
        self._scroll_events_into_view()
        if self._preview_visible:
            self._update_preview()

    def _scroll_events_into_view(self) -> None:
        """Scroll #panel-scroll so the events cursor line is visible.

        Events render with variable height: chain switches insert a
        blank separator line between groups, and
        ``user_message_received`` rows have an extra ``↳`` reply line
        below the headline. The pre-A-F3 arithmetic projection
        (``y = 1 + cursor``) under-scrolled by the count of intervening
        chain-switch + reply lines, so after a few chains the cursor
        sat below the viewport with no visible movement on j/k.

        ``render_events`` records the exact y of each event's
        headline row in ``_events_event_ys``; we look it up here.
        Same idiom as the memory- and agents-tab fixes.
        """
        try:
            vs = self.query_one("#panel-scroll", VerticalScroll)
            current = int(vs.scroll_y)
            visible = vs.size.height
            if visible <= 0:
                return
            if not (0 <= self._events_cursor < len(self._events_event_ys)):
                return
            # +1 for content padding-top, matching the memory-tab fix.
            y = 1 + self._events_event_ys[self._events_cursor]
            if y < current:
                vs.scroll_to(y=y, animate=False)
            elif y >= current + visible:
                vs.scroll_to(y=y - visible + 1, animate=False)
        except Exception as exc:
            logger.warning("right_panel scroll_events_into_view failed: %s", exc)

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
        self._scroll_memory_into_view()
        if self._preview_visible:
            self._update_preview()

    def _scroll_memory_into_view(self) -> None:
        """Scroll #panel-scroll so the memory cursor row is visible.

        Memory entries render with variable structure (section headers,
        per-type subheaders, optional description rows, blank separators),
        so an arithmetic ``y = f(cursor)`` would be wrong as soon as a
        type group changes. ``render_memory`` records the exact y of each
        entry's name row in ``_memory_entry_ys``; we just look it up here.

        Same idiom as the events-tab fix in PR #97.
        """
        try:
            vs = self.query_one("#panel-scroll", VerticalScroll)
            current = int(vs.scroll_y)
            visible = vs.size.height
            if visible <= 0:
                return
            if not (0 <= self._memory_cursor < len(self._memory_entry_ys)):
                return
            # +1 for content padding-top, matching the events-tab fix.
            y = 1 + self._memory_entry_ys[self._memory_cursor]
            if y < current:
                vs.scroll_to(y=y, animate=False)
            elif y >= current + visible:
                vs.scroll_to(y=y - visible + 1, animate=False)
        except Exception as exc:
            logger.warning("right_panel scroll_memory_into_view failed: %s", exc)

    def _show_pending_in_preview(self, pane: _PreviewPane) -> None:
        """Render the cursor's pending intervention as structured text.

        A-F1 (wave-8): before this preview path, ``Space`` on the
        pending tab silently did nothing — ``_has_previewable_content``
        returned False and the preview-pane open was suppressed. The
        user had to claim the intervention to a local channel just to
        see the full prompt detail. Now the preview shows the same
        fields the list row carries (kind / id / origin / age / summary)
        plus the full ``detail`` body which the row truncates.
        """
        if not self._pending_items:
            pane.clear()
            return
        idx = max(0, min(len(self._pending_items) - 1, self._pending_cursor))
        item = self._pending_items[idx]
        from rich.console import Group as RichGroup
        from rich.text import Text as RichText
        head = RichText()
        kind = str(item.get("kind") or "?")
        iv_id = str(item.get("id") or "")
        head.append(kind, style="bold " + _CORAL)
        if iv_id:
            head.append("  ")
            head.append(iv_id[:8], style="dim")
        # Provenance fields go on subsequent lines as ``key: value`` for
        # scan-ability — matches the YAML idiom used by the events
        # preview without the full YAML overhead.
        head.append("\n")
        origin = str(item.get("origin_channel_id") or "")
        if origin:
            head.append("origin: ", style="dim #888888")
            head.append(origin, style="#aaaaaa")
            head.append("\n")
        created = str(item.get("created_at") or "")
        if created:
            head.append("created_at: ", style="dim #888888")
            head.append(created, style="#aaaaaa")
            head.append("\n")
        summary = str(item.get("summary") or "")
        if summary:
            head.append("\n")
            head.append(summary, style="#dddddd")
            head.append("\n")
        detail = str(item.get("detail") or "")
        body = RichText(detail, style="dim") if detail else RichText("")
        title = iv_id[:8] if iv_id else kind
        pane.show_text(title, RichGroup(head, body))

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

    # ── keys tab cursor (T1-4, Wave-12) ─────────────────────────────────────

    def _keys_move(self, delta: int) -> None:
        """Move the keys-tab cursor by ``delta`` and re-render.

        The flat row count is derived from a speculative ``render_keys`` call
        so ``keys_move`` knows the list length for wrapping. This is cheap
        (= no IO, pure string construction) and keeps the cursor consistent
        with whatever rows ``render_keys`` would actually emit.
        """
        _, flat_key_list, key_ys = render_keys(
            self.app,
            cursor=get_keys_cursor(),
            expanded=get_keys_expanded(),
        )
        keys_move(delta, len(flat_key_list))
        self._key_ys = key_ys
        self._invalidate()
        self._scroll_keys_into_view()

    # ── pending tab cursor + actions (issue #277) ────────────────────────────

    def _pending_move(self, delta: int) -> None:
        """Move the Pending tab cursor; wrap around modulo list length.

        A-F1 (wave-8): if preview pane is open, sync to the new cursor's
        intervention — same pattern as ``_events_move`` / ``_memory_move``.
        """
        n = len(self._pending_items)
        if n == 0:
            self._pending_cursor = 0
            return
        self._pending_cursor = (self._pending_cursor + delta) % n
        self._invalidate()
        self._scroll_pending_into_view()
        if self._preview_visible:
            self._update_preview()

    def _pending_action_discard(self) -> None:
        """Discard the iv at the current cursor.

        Calls ``ChatSession.discard_pending_intervention``. Flashes
        a short status confirmation in the conv pane on success;
        silently no-ops when the cursor isn't on a valid row.
        """
        if not (0 <= self._pending_cursor < len(self._pending_items)):
            return
        item = self._pending_items[self._pending_cursor]
        iv_id = str(item.get("id") or "")
        if not iv_id:
            return
        try:
            session = self.app._get_session()  # type: ignore[attr-defined]
        except Exception:
            session = None
        if session is None:
            self._flash_status(
                "pending: discard unavailable (no session)",
            )
            return
        import asyncio
        asyncio.create_task(
            self._pending_run_action(
                session.discard_pending_intervention(iv_id),
                ok_msg=f"discarded {iv_id[:8]}",
                fail_msg=f"discard failed: {iv_id[:8]}",
            )
        )

    def _pending_action_claim(self) -> None:
        """Claim the iv at the current cursor for the local TUI channel."""
        if not (0 <= self._pending_cursor < len(self._pending_items)):
            return
        item = self._pending_items[self._pending_cursor]
        iv_id = str(item.get("id") or "")
        if not iv_id:
            return
        try:
            session = self.app._get_session()  # type: ignore[attr-defined]
        except Exception:
            session = None
        if session is None:
            self._flash_status("pending: claim unavailable (no session)")
            return
        # Channel id naming follows the issue #268 convention
        # (= ``tui:<session-tag>``); we use the agent name as the tag
        # since the TUI has 1-agent-attached at a time.
        channel_id = f"tui:{getattr(session, 'agent_name', 'default')}"
        import asyncio
        asyncio.create_task(
            self._pending_run_action(
                session.claim_pending_intervention(iv_id, channel_id),
                ok_msg=f"claimed {iv_id[:8]}",
                fail_msg=f"claim failed: {iv_id[:8]}",
            )
        )

    async def _pending_run_action(
        self, coro, *, ok_msg: str, fail_msg: str,
    ) -> None:
        """Run a discard / claim coroutine + flash result to status."""
        try:
            result = await coro
        except Exception as exc:
            logger.warning("pending tab action failed: %s", exc)
            self._flash_status(fail_msg)
            return
        if result:
            self._flash_status(ok_msg)
            self._invalidate()
        else:
            self._flash_status(fail_msg)

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
        self._scroll_agents_into_view()
        if self._preview_visible:
            self._update_preview()

    def _scroll_agents_into_view(self) -> None:
        """Scroll #panel-scroll so the agents cursor row is visible.

        Agents tab uses a RichTree per agent; each running skill / plan
        / recent row spans 1-2 lines (a child node like a phase or goal
        adds a second). ``render_agents`` records the exact y of each
        selectable item in ``_agents_item_ys`` so we don't have to guess.

        Same idiom as the events- and memory-tab fixes (PR #97 + this PR).
        """
        try:
            vs = self.query_one("#panel-scroll", VerticalScroll)
            current = int(vs.scroll_y)
            visible = vs.size.height
            if visible <= 0:
                return
            if not (0 <= self._agents_cursor < len(self._agents_item_ys)):
                return
            # +1 for content padding-top, matching the events-tab fix.
            y = 1 + self._agents_item_ys[self._agents_cursor]
            if y < current:
                vs.scroll_to(y=y, animate=False)
            elif y >= current + visible:
                vs.scroll_to(y=y - visible + 1, animate=False)
        except Exception as exc:
            logger.warning("right_panel scroll_agents_into_view failed: %s", exc)

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
        elif kind == "agent":
            # Wave-10 H-F1: agent-label row preview. Pre-fix this branch
            # fell through to ``pane.clear()``, leaving the preview pane
            # blank — the user saw Space "work" (pane layout shifted)
            # but got no content, a silent no-op confusing for a tab
            # whose other rows render rich previews. Now we show a
            # compact summary (name + status + attached/loaded flags +
            # in-flight counts) so opening an agent row tells the user
            # what that agent is doing right now.
            self._preview_agent(pane, item)
        else:
            pane.clear()

    def _preview_agent(self, pane: _PreviewPane, item: dict) -> None:
        """Compact summary for an agent-label row (H-F1, wave-10).

        Renders:
          - name (bold coral if attached, else white)
          - status (running / ready / idle) derived from the live
            ``_exec_state`` (= running skills) and the
            ``loaded`` flag on the flat-item
          - count of in-flight skills / plans
          - "attached" / "loaded" boolean status

        Sources are the same ones ``render_agents`` uses to build the
        agent-label row, so the preview never disagrees with the
        tree line above it.
        """
        from rich.console import Group as RichGroup
        from rich.text import Text as RichText
        name = str(item.get("name", "?"))
        is_attached = bool(item.get("attached", False))
        is_loaded = bool(item.get("loaded", False))
        # Count in-flight skills for this agent from the live state.
        running_skills = [
            rid for rid, info in self._exec_state.items()
            if info.get("agent_name") == name
        ]
        has_work = bool(running_skills)
        if has_work:
            status_glyph, status_text, status_style = (
                "● ", "running", "#44cc88",
            )
        elif is_loaded:
            status_glyph, status_text, status_style = (
                "◐ ", "ready", "#aaaa55",
            )
        else:
            status_glyph, status_text, status_style = (
                "○ ", "idle", "#555555",
            )
        head = RichText()
        head.append(name, style=("bold " + _CORAL) if is_attached else "#dddddd")
        head.append("  ")
        head.append(status_glyph + status_text, style=status_style)
        head.append("\n")
        head.append("attached: ", style="dim")
        head.append("yes" if is_attached else "no", style="#dddddd")
        head.append("\n")
        head.append("loaded: ", style="dim")
        head.append("yes" if is_loaded else "no", style="#dddddd")
        head.append("\n")
        head.append("running skills: ", style="dim")
        head.append(str(len(running_skills)), style="#dddddd")
        if running_skills:
            head.append("\n")
            head.append("  ", style="dim")
            head.append(
                ", ".join(rid[:8] for rid in running_skills[:4]),
                style="dim #aaaaaa",
            )
            if len(running_skills) > 4:
                head.append(f", +{len(running_skills) - 4} more", style="dim")
        pane.show_text(name, RichGroup(head))

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

    def _prefill_attach_for_cursor(self) -> None:
        """`a` on the Agents tab — pre-fill ``/attach <cursor name>``.

        Wave-10 follow-up H-F11. Reads the cursor's flat_item; when
        the row is an ``"agent"`` kind, prefills the InputBar with
        ``/attach <agent-name>``. On any other cursor (= running /
        recent skill / plan rows), the prefill is silently skipped
        — the action is only meaningful on the agent-label row.

        Switching to the currently-attached agent is harmless (= the
        slash command treats it as a no-op), so the test for "this
        is the attached agent already" lives in the slash command,
        not here.
        """
        if not self._agents_items:
            return
        idx = max(0, min(len(self._agents_items) - 1, self._agents_cursor))
        item = self._agents_items[idx]
        if item.get("kind") != "agent":
            # Wave-11 A#6: walk UP through the flat list to the nearest
            # agent-label row. The agents tab orders items per-agent
            # (header row + that agent's running / recent items), so
            # the first ``kind == "agent"`` above the cursor is the
            # owning agent. Previously this was a silent no-op which
            # gave the user no feedback (= "is the key broken?"); now
            # we prefill the owning agent's /attach so the user gets
            # a meaningful result regardless of which sub-row they
            # landed on.
            owning: dict | None = None
            for upstream in reversed(self._agents_items[:idx]):
                if upstream.get("kind") == "agent":
                    owning = upstream
                    break
            if owning is None:
                # No agent header found above cursor — surface a hint
                # so the user knows the key did something (= they're
                # likely on a malformed / empty agents tab; rare).
                self._flash_status("a: no owning agent for this row")
                return
            item = owning
        name = str(item.get("name", "")).strip()
        if not name:
            return
        from .. import InputBar  # late import → avoid cycle
        try:
            inputbar = self.app.query_one("#inputbar", InputBar)
        except Exception as exc:
            logger.warning("right_panel prefill attach failed: %s", exc)
            return
        prefill = f"/attach {name}"
        try:
            ta = inputbar.query_one("#input")
            ta.load_text(prefill)
            # Move cursor to end so the user can Enter immediately or
            # edit (= rename agent, etc.) without skipping whitespace.
            ta.move_cursor((0, len(prefill)))
            inputbar.focus_input()
        except Exception as exc:
            logger.warning("right_panel prefill attach focus failed: %s", exc)

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

    def _jump_docs_to_events_md(self) -> None:
        """Events tab [d] — switch to Docs tab, cursor on runtime/events.md.

        T2-5a (Wave-12): closes the navigation gap between the events tab
        (the densest jargon surface) and the runtime/events.md reference.
        Builds the docs index from the current project root, finds the first
        file whose stem equals ``events`` inside the ``reference/runtime``
        section (or any section), sets the cursor there, then activates the
        Docs tab. Falls through silently when docs/ or the target file are
        absent — the user stays on the events tab.
        """
        groups, ordered = build_docs_index(
            self._project_root, "", lang=self._docs_lang,
        )
        if not ordered:
            self._flash_status("docs/: not found")
            return
        # Find index of the file whose stem is "events" under reference/runtime
        # (preferred) or anywhere in the flat list (fallback).
        target_idx: int | None = None
        for i, path in enumerate(ordered):
            if path.stem == "events" and "runtime" in str(path):
                target_idx = i
                break
        if target_idx is None:
            # Fallback: first file named events.md anywhere
            for i, path in enumerate(ordered):
                if path.stem == "events":
                    target_idx = i
                    break
        if target_idx is None:
            self._flash_status("runtime/events.md: not found in docs/")
            return
        self._docs_groups = groups
        self._docs_files = ordered
        self._docs_cursor = target_idx
        self.set_panel_type("docs")

    def current_doc_stem(self) -> str:
        """Return the stem of the currently highlighted doc file, or "".

        Public accessor for tests (and future callers) that need to verify
        the Docs tab cursor position without reading private state directly.
        """
        if not self._docs_files:
            return ""
        idx = max(0, min(len(self._docs_files) - 1, self._docs_cursor))
        return self._docs_files[idx].stem

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
        # Wave-10 follow-up H-F6: prefer ``plan_id_full`` (= the
        # canonical UUID) when it's present so the copied payload
        # matches the events log identifier exactly. Falls back to
        # the 8-char display prefix if the flat_item lacks the full
        # form — defensive against older callers.
        plan_id = item.get("plan_id_full") or item.get("plan_id", "?")
        lines.append(f"# Reyn plan (running) · {plan_id}")
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

    def _scroll_pending_into_view(self) -> None:
        """Scroll #panel-scroll so the Pending tab cursor is visible.

        Issue #277 — Pending tab j/k navigation. Uses the same shape
        as the events / agents / memory / docs helpers and integrates
        with the ``on_tabs_tab_activated`` dispatch table established
        in TUI Right Panel exploration wave (PR #231).
        """
        try:
            vs = self.query_one("#panel-scroll", VerticalScroll)
            current = int(vs.scroll_y)
            visible = vs.size.height
            if visible <= 0:
                return
            if not (0 <= self._pending_cursor < len(self._pending_item_ys)):
                return
            y = 1 + self._pending_item_ys[self._pending_cursor]
            if y < current:
                vs.scroll_to(y=y, animate=False)
            elif y >= current + visible:
                vs.scroll_to(y=y - visible + 1, animate=False)
        except Exception as exc:
            logger.warning(
                "right_panel scroll_pending_into_view failed: %s", exc,
            )

    def _scroll_keys_into_view(self) -> None:
        """Scroll #panel-scroll so the Keys tab cursor is visible.

        Uses the same shape as ``_scroll_pending_into_view`` /
        ``_scroll_events_into_view``.  ``_key_ys`` is populated by
        ``render_keys`` (via ``_keys_move`` and ``_panel_markup``) and
        records the 0-indexed rendered-output line of each key row.
        """
        try:
            vs = self.query_one("#panel-scroll", VerticalScroll)
            current = int(vs.scroll_y)
            visible = vs.size.height
            if visible <= 0:
                return
            cursor = get_keys_cursor()
            if not (0 <= cursor < len(self._key_ys)):
                return
            y = 1 + self._key_ys[cursor]
            if y < current:
                vs.scroll_to(y=y, animate=False)
            elif y >= current + visible:
                vs.scroll_to(y=y - visible + 1, animate=False)
        except Exception as exc:
            logger.warning(
                "right_panel scroll_keys_into_view failed: %s", exc,
            )

    # ── render dispatch ───────────────────────────────────────────────────────

    def _panel_header_markup(self) -> str:
        if self._panel_type == "keys":
            return f"[bold {_CORAL}]Key Bindings[/]  [#555555]j↓ k↑[/]"
        if self._panel_type == "agents":
            # Wave-10 H-F2: surface ``space=open c=copy`` so the cursor's
            # most useful actions are discoverable from the header.
            # Wave-10 follow-up H-F11: also surface ``a=attach`` — the
            # agents tab's per-tab action that prefills ``/attach
            # <name>`` for switching the attached agent. Matches the
            # Pending tab's ``d=discard c=claim`` per-tab hint shape.
            return (
                f"[bold {_CORAL}]Agents[/]"
                f"  [#555555]j↓ k↑ space=open c=copy a=attach[/]"
            )
        if self._panel_type == "memory":
            return (
                f"[bold {_CORAL}]Memory[/]"
                f"  [#555555]j↓ k↑ space=open c=copy[/]"
            )
        if self._panel_type == "cost":
            return f"[bold {_CORAL}]Cost[/]  [#555555]j↓ k↑[/]"
        if self._panel_type == "docs":
            return (
                f"[bold {_CORAL}]Docs[/]"
                f"  [#555555]j↓ k↑ space=open /=filter g=lang[/]"
            )
        if self._panel_type == "pending":
            # Issue #277 — Pending tab keybinds: j/k cursor +
            # ``d`` discard + ``c`` claim. Mirrors the Memory tab
            # 1-key action idiom (= `c=copy`).
            # A-F1 (wave-8): sp=open surfaces the intervention's full
            # detail in the preview pane, same idiom as events / memory.
            return (
                f"[bold {_CORAL}]Pending[/]"
                f"  [#555555]j↓ k↑ sp=open d=discard c=claim[/]"
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
            # Compact form (~36 cells with "all" filter, 8 more for the
            # longest "internal" filter). Previous text was ~54 cells and
            # truncated past ``[t`` at the new 36-col minimum panel width
            # (= the user lost the entire keybind half of the header).
            # Trade-off:
            #   * drop the ``ilter:`` / ``ail:`` glue text — the
            #     ``[f]:<name>`` / ``[t]:<n>`` shape still reads naturally
            #   * drop ``=move`` / ``space=open`` glue — ``j/k`` and
            #     ``sp=open`` carry the same information in fewer cells,
            #     matching the docs / memory tab idiom
            return (
                f"[bold {_CORAL}]Events[/]"
                f"  {kf}[#555555]:[/]{filter_label}"
                f"  {kt}[#555555]:[/][#aaaaaa]{tail}[/]"
                f"  [#555555]j/k sp=open[/]"
            )
        return ""

    def _panel_markup(self) -> Any:
        try:
            if self._panel_type == "keys":
                markup, _, key_ys = render_keys(
                    self.app,
                    cursor=get_keys_cursor(),
                    expanded=get_keys_expanded(),
                )
                self._key_ys = key_ys
                return markup
            if self._panel_type == "events":
                rendered, windowed, event_ys = render_events(
                    self._project_root,
                    self._event_filter_idx,
                    self._event_tail_idx,
                    cursor=self._events_cursor,
                    cache=self._events_cache,
                    filelist_cache=self._events_filelist_cache,
                    chain_isolate=self._events_chain_isolate,
                    verbose=self._events_verbose,
                )
                self._events_visible = windowed
                self._events_event_ys = event_ys
                if self._events_cursor >= len(windowed):
                    self._events_cursor = max(0, len(windowed) - 1)
                return rendered
            if self._panel_type == "agents":
                rendered, flat_items, item_ys = render_agents(
                    self._registry, self._exec_state,
                    project_root=self._project_root,
                    cursor=self._agents_cursor,
                )
                self._agents_items = flat_items
                self._agents_item_ys = item_ys
                if self._agents_cursor >= len(flat_items):
                    self._agents_cursor = max(0, len(flat_items) - 1)
                return rendered
            if self._panel_type == "memory":
                rendered, flat_entries, entry_ys = render_memory(
                    self._project_root,
                    cursor=self._memory_cursor,
                    hot_list=self._hot_list_ranking,
                    type_filter=self._memory_type_filter,
                )
                self._memory_entries = flat_entries
                self._memory_entry_ys = entry_ys
                if self._memory_cursor >= len(flat_entries):
                    self._memory_cursor = max(0, len(flat_entries) - 1)
                return rendered
            if self._panel_type == "cost":
                budget_tracker = getattr(self.app, "_budget_tracker", None)
                return render_cost(self._project_root, budget_tracker)
            if self._panel_type == "docs":
                groups, ordered = build_docs_index(
                    self._project_root, self._docs_filter,
                    lang=self._docs_lang,
                )
                self._docs_groups = groups
                self._docs_files = ordered
                if self._docs_cursor >= len(ordered):
                    self._docs_cursor = max(0, len(ordered) - 1)
                return render_docs(
                    self._project_root, self._docs_cursor, groups,
                    docs_filter=self._docs_filter,
                    lang=self._docs_lang,
                )
            if self._panel_type == "pending":
                # Issue #277 — surface stalled / cross-channel ops.
                # The session API ``list_stalled_interventions`` returns
                # ``list[PendingOpView]``; we coerce to dicts in
                # ``render_pending`` so dataclass / dict callers both work.
                pending_ops: list = []
                remote_mode = False
                try:
                    session = self.app._get_session()  # type: ignore[attr-defined]
                except Exception:
                    session = None
                if session is None:
                    # ``--connect`` (Phase A of #276) mode currently has
                    # no local session — scoped disable per #277 + #276
                    # Phase C-(b). When Phase C-(a) lands REST fetch,
                    # this branch becomes the REST call site.
                    remote_mode = True
                else:
                    try:
                        pending_ops = session.list_stalled_interventions()
                    except Exception as exc:
                        logger.warning(
                            "pending tab: list_stalled_interventions failed: %s",
                            exc,
                        )
                        pending_ops = []
                rendered, flat_items, item_ys = render_pending(
                    pending_ops,
                    cursor=self._pending_cursor,
                    remote_mode=remote_mode,
                )
                self._pending_items = flat_items
                self._pending_item_ys = item_ys
                if self._pending_cursor >= len(flat_items):
                    self._pending_cursor = max(0, len(flat_items) - 1)
                return rendered
        except Exception as e:
            logger.warning("right_panel render dispatch failed: %s", e)
            return f"[red]error: {_esc(str(e))}[/red]"
        return ""


__all__ = ["RightPanel", "PANEL_TYPES"]
