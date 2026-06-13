"""ReynTUIApp — Textual TUI for `reyn chat`.

Layout:
  ┌─ ReynHeader ─────────────────────────────────────────────────────┐  dock=top h=1
  │                                                                   │
  │  ConversationView (RichLog + inline widgets)  1fr  │  RightPanel │  dock=right (hidden by default)
  │                                                                   │
  ├───────────────────────────────────────────────────────────────────┤
  │  InputBar (TextArea + hint label)                     dock=bottom │
  └───────────────────────────────────────────────────────────────────┘

RightPanel (ctrl+b to toggle, ctrl+o to focus, ctrl+w to cycle tabs):
  keys · events · agents · memory · docs

ChatSession integration (phase 3+):
  - subscribe_outbox():    coroutine draining registry.repl_outbox → render
  - on input submit:       call session.submit_user_text(text)
  - slash dispatch:        REGISTRY.get(cmd).handler(session, args)
  - intervention answer:   session._deliver_answer_to(iv, text)

Status line (phase 6):
  After every model call, fetch budget_tracker.snapshot() and call
  header.refresh_status(...).

This file is the composition root; widget logic stays in widgets/.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic as _now_monotonic
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.theme import Theme

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui._palette import (
    _BORDER_DIM,
    _CORAL,
    _RED_MUTED,
    _TEXT_BODY,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    css_variables,
)

from .widgets import ConversationView, InputBar, InterventionWidget, ReynHeader, RightPanel

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession


# Debounce window for the "nothing-in-flight cancel" line — repeat
# Ctrl+C presses within this many seconds are absorbed silently.
_IDLE_CANCEL_DEDUP_S = 1.5

# Wave-6 ST3: cost-suffix deferral while skills / plans are still
# spinning. We retry the snapshot every ``INTERVAL_S`` for up to
# ``MAX_ATTEMPTS`` total seconds before falling back to a write
# with whatever the budget tracker shows. The cap prevents an
# infinitely-running skill from suppressing the cost line forever.
_COST_SUFFIX_DEFER_INTERVAL_S = 1.0
_COST_SUFFIX_DEFER_MAX_ATTEMPTS = 30

# #1546: max gap between the two clean Escs of an Esc-Esc → /rewind double-tap.
# 600ms (tui-coder UX call) gives the "Esc again to rewind" hint time to be
# read while staying tight enough to not fire on unrelated paired Escs.
_ESC_ESC_WINDOW_S = 0.6


class ReynTUIApp(App):
    """Main Textual application for `reyn chat`."""

    CSS_PATH = Path(__file__).parent / "theme.tcss"

    # Default terminal window title — shown in the tab bar of multiplexers
    # (tmux, iTerm2 tabs, gnome-terminal). Defaults to the class name
    # (``"ReynTUIApp"``) if left unset, which leaks an implementation
    # detail. ``set_title_state`` swaps this to ``reyn — awaiting answer``
    # / ``reyn — error`` etc. so a user in a background terminal can see
    # the state without focusing the window.
    TITLE = "reyn"

    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+d", "quit_tui", "Quit", priority=True),
        Binding("ctrl+l", "clear_conversation", "Clear", priority=True),
        Binding("ctrl+c", "cancel_inflight", "Cancel", priority=True),
        Binding("ctrl+b", "toggle_panel", "Panel", priority=True, show=False),
        Binding("ctrl+o", "focus_toggle_panel", "Focus panel", priority=True, show=False),
        # Right-panel tab cycling. The action methods
        # (``action_panel_next_content`` / ``_prev_content``) already exist
        # but were missing Binding declarations, so the keys never fired AND
        # the Keys tab — which iterates ``app.BINDINGS`` — never rendered
        # them. ``ctrl+shift+o`` is an alias for prev-tab; some terminals
        # don't deliver ``ctrl+shift+w`` reliably, so the alias is the
        # escape hatch.
        Binding("ctrl+w", "panel_next_content", "Next tab", priority=True, show=False),
        Binding("ctrl+shift+w", "panel_prev_content", "Prev tab", priority=True, show=False),
        Binding("ctrl+shift+o", "panel_prev_content", "Prev tab (alt)", priority=True, show=False),
        # Ctrl+1 .. Ctrl+7 — jump directly to the Nth right-panel tab
        # (browser / IDE tab convention). Opens the panel if hidden so
        # the user gets one-keypress access from a closed-panel state.
        # Order matches ``PANEL_TYPES`` so Ctrl+N corresponds to the
        # visual tab order — Ctrl+1=Keys, Ctrl+2=Events, …, Ctrl+7=Pending.
        Binding("ctrl+1", "panel_jump_keys", "Jump to Keys tab", priority=True, show=False),
        Binding("ctrl+2", "panel_jump_events", "Jump to Events tab", priority=True, show=False),
        Binding("ctrl+3", "panel_jump_agents", "Jump to Agents tab", priority=True, show=False),
        Binding("ctrl+4", "panel_jump_memory", "Jump to Memory tab", priority=True, show=False),
        Binding("ctrl+5", "panel_jump_cost", "Jump to Cost tab", priority=True, show=False),
        Binding("ctrl+6", "panel_jump_docs", "Jump to Docs tab", priority=True, show=False),
        Binding("ctrl+7", "panel_jump_pending", "Jump to Pending tab", priority=True, show=False),
        Binding("ctrl+p", "prev_turn", "Prev turn", priority=True, show=False),
        Binding("ctrl+n", "next_turn", "Next turn", priority=True, show=False),
        Binding("f", "event_filter_cycle", "Filter events (events tab)", priority=True, show=False),
        Binding("t", "event_tail_cycle", "Tail events (events tab) / memory filter (memory tab)", priority=True, show=False),
        # Primary voice toggle: ctrl+r (Record). F2 kept as alias because it
        # ships with the user guide, but many terminals — and macOS by default
        # — intercept F-keys before they reach Textual.
        Binding("ctrl+r", "voice_toggle", "Voice", priority=True, show=False),
        Binding("f2", "voice_toggle", "Voice (alias)", priority=True, show=False),
        # Enter while recording = stop + transcribe + submit immediately.
        # Lets the user dictate, decide it's clean, and send without
        # touching the keyboard for the edit step. Gated by check_action
        # so plain Enter in the input bar still does its normal submit.
        Binding("enter", "voice_stop_and_submit", "Voice send", priority=True, show=False),
        # Esc multiplexes: cancel voice recording / close right panel /
        # close slash picker / dismiss rewind menu — see ``action_voice_cancel``.
        Binding("escape", "voice_cancel", "Cancel / close", priority=True, show=False),
        # ADR-0038 1f: rewind-menu navigation. Priority + check_action-gated to
        # when the /rewind picker is open, so plain ↑/↓/Enter fall through to
        # the InputBar (history recall / submit) at all other times — same
        # gating discipline as ``voice_stop_and_submit`` (Enter) above.
        Binding("up", "rewind_prev", "Rewind: prev checkpoint", priority=True, show=False),
        Binding("down", "rewind_next", "Rewind: next checkpoint", priority=True, show=False),
        Binding("enter", "rewind_confirm", "Rewind: select checkpoint", priority=True, show=False),
        Binding("ctrl+backslash", "screenshot", "Screenshot", priority=True, show=False),
        # Wave-4 AR5: keyboard scroll for the conv log. RichLog has
        # ``can_focus=False`` (intentional — prevents inadvertent
        # focus capture from input), so Textual's default Page
        # Up/Down don't reach it. Bind at app level + dispatch
        # through ``conv.scroll_page_up`` / ``scroll_page_down``
        # without transferring focus, so the user can scroll
        # back through history with the keyboard alone (= no mouse
        # required, complement to the existing anchor-based Ctrl+P/N).
        Binding("pageup", "conv_scroll_page_up", "Scroll up", priority=True, show=False),
        Binding("pagedown", "conv_scroll_page_down", "Scroll down", priority=True, show=False),
        # Line-granularity + jump-to-tail scroll keys. Alt-prefixed so
        # they don't collide with the input bar's Up/Down history bindings
        # or with TextArea's End=end-of-line. Complements the existing
        # page-step keys when a PageUp overshoots and the user wants to
        # nudge the viewport by a single line. Alt+End re-arms auto-scroll
        # so the next streaming message follows along again without
        # waiting for a manual PageDown chain.
        Binding("alt+up", "conv_scroll_line_up", "Scroll up 1 line", priority=True, show=False),
        Binding("alt+down", "conv_scroll_line_down", "Scroll down 1 line", priority=True, show=False),
        Binding("alt+home", "conv_scroll_home", "Scroll to top", priority=True, show=False),
        Binding("alt+end", "conv_scroll_end", "Scroll to bottom", priority=True, show=False),
        # ``/find`` cycle navigation. After ``/find <query>`` lands the
        # first match, Ctrl+G cycles forward and Ctrl+Shift+G cycles
        # backward through the remaining matches (with wrap). When no
        # prior ``/find`` query is set, the action surfaces a usage hint
        # rather than silently no-op'ing. ``priority=True`` so the keys
        # win against any widget-level binding.
        Binding("ctrl+g", "find_next", "Find next match", priority=True, show=False),
        Binding("ctrl+shift+g", "find_prev", "Find prev match", priority=True, show=False),
        # Keyboard companion to the mouse-click skill-row drill-down
        # (= toggle phase-history expand). F3 avoids the Ctrl+letter
        # collision with TextArea's default editing shortcuts (ctrl+e
        # end-of-line, ctrl+a start-of-line, etc.). Target is every
        # in-flight skill row — typically one row, but if multiple
        # skills are concurrent the user can expand them all at once
        # with one keypress. Status hint when nothing is running.
        Binding("f3", "skill_expand_toggle", "Toggle skill row drill-down", priority=True, show=False),
        # Focus the bottom AsyncStackPanel for keyboard navigation
        # (= F4 → panel focus → j/k navigate → c prefills /cancel <id>
        # → Esc returns to InputBar). Status hint when the panel
        # has no entries (= nothing to focus). F-key chosen over
        # Ctrl+letter to avoid clobbering TextArea editing defaults.
        Binding("f4", "focus_async_stack", "Focus async strip", priority=True, show=False),
        # W13 T2-1: F7 keyboard drill for the most-recent failed ToolCallRow.
        # F4 is reserved for async-stack panel focus (Wave-9 / keys_tab
        # description). F5/F6 are FREE since #1217 removed the conv-pane
        # error-jump (#586) — available for reuse.
        # F7 follows historically.
        Binding("f7", "drill_failed_tool", "Toggle most-recent failed tool row", priority=True, show=False),
        # F9: toggle timestamp prefix (HH:MM) in conv pane headers.
        # With ts hidden, body indent shrinks col 8 → col 2, reclaiming
        # horizontal space for content. State persists via tui_prefs.json.
        Binding("f9", "toggle_timestamps", "Toggle timestamps", priority=True, show=False),
    ]

    _REYN_THEME = Theme(
        name="reyn",
        primary=_CORAL,
        accent=_CORAL,
        dark=True,
        variables={
            "block-cursor-background": _CORAL,
            "block-cursor-foreground": "#ffffff",  # palette-candidate: white foreground on coral cursor
        },
    )

    def get_css_variables(self) -> dict[str, str]:
        """Inject the palette as ``$reyn-*`` CSS variables.

        Lets ``theme.tcss`` (and any ``.tcss``) reference ``_palette.py``
        directly instead of hand-syncing hex literals — ``_palette`` becomes
        the single source for CSS-side colours too. Merges on top of
        Textual's built-in theme variables ($primary, $surface, …).
        """
        return {**super().get_css_variables(), **css_variables()}

    def __init__(
        self,
        *,
        registry: "AgentRegistry | None" = None,
        agent_name: str = "default",
        model: str = "",
        budget_tracker=None,
        banner: bool = False,
        no_restore: bool = False,
    ) -> None:
        super().__init__()
        self.register_theme(self._REYN_THEME)
        self.theme = "reyn"
        self._agent_registry = registry   # NOTE: NOT _registry (Textual internal)
        self._agent_name = agent_name
        self._model = model
        self._budget_tracker = budget_tracker
        self._banner = banner
        self._no_restore = no_restore
        self._outbox_task: asyncio.Task | None = None
        # OutboxRouter instance, kept on the app so actions outside the
        # outbox loop (= Ctrl+G / Ctrl+Shift+G ``/find`` cycle navigation)
        # can reach the find-cycle state living on the router.
        self._outbox_router = None  # type: ignore[assignment]
        self._panel_visible = False
        # ADR-0038 1f: the mounted RewindMenuWidget while the /rewind picker is
        # open, else None. Navigation (↑/↓/Enter) + Esc dismiss are gated on
        # this being non-None via check_action.
        self._rewind_menu = None  # type: ignore[assignment]
        # #1546: monotonic ts of the last *truly clean* Esc (nothing dismissed).
        # A second clean Esc within _ESC_ESC_WINDOW_S opens the /rewind picker
        # (Esc-Esc double-tap). 0.0 = no pending first tap. Any Esc that
        # dismisses something resets this so "dismiss then clean-Esc" cannot
        # masquerade as a double-tap.
        self._last_clean_esc_ts: float = 0.0
        self._esc_hint_timer = None  # type: ignore[assignment]
        self._cancel_event: asyncio.Event = asyncio.Event()
        # Most-recent "nothing-in-flight cancel" timestamp, used to
        # suppress repeated identical lines from accumulating in the conv
        # log when the user mashes Ctrl+C on an idle session.
        self._last_idle_cancel_ts: float = 0.0
        # run_id → {skill_name, agent_name, start_time, phase, phase_visits}
        self._skill_exec: dict[str, dict] = {}
        # Per-turn cost tracking (A4 — opt-in via /cost-inline)
        self._cost_inline_enabled = False
        self._turn_start_cost_usd: float = 0.0
        self._turn_start_tokens: int = 0
        self._turn_start_time: float = 0.0
        # Recent context for smart Ctrl+B target tab
        self._last_focal_tab: str | None = None  # "events" | "agents" | None
        # Active streaming row id — shared between __stream_* handlers
        self._current_stream_id: str | None = None
        # Most-recent user-typed message (or voice-dictated transcript) —
        # captured into ``_skill_exec[run_id]['triggered_by']`` the first
        # time we see a trace event for a new run, so the agents tab
        # preview can show "which user message kicked off this skill?".
        self._latest_user_message: str = ""
        # Persistent map of run_id → triggered_by message, kept across
        # `_on_skill_done` (which pops `_skill_exec` once a run finishes).
        # Lets the recent_skill preview display the same triggered_by
        # info even after the skill row has rotated out of the running
        # set. Bounded by a soft cap below to keep memory predictable
        # in long-running sessions.
        self._run_id_to_user_message: dict[str, str] = {}
        # Voice input (lazy — created on first F2 press so import-time cost is
        # zero for users who don't have the `reyn[voice]` extras installed).
        self._voice_input = None  # type: ignore[var-annotated]
        self._voice_busy: bool = False  # True while transcription is running

    @property
    def panel_visible(self) -> bool:
        """Public read of the right-panel visibility state.

        Returns ``True`` when the panel is currently shown, ``False``
        when hidden. Use ``action_toggle_panel()`` or the panel-jump
        actions to mutate — direct assignment to ``_panel_visible`` is
        not part of the public contract.

        Exposed for tests (and callers like ``_dispatch_pending_click``)
        so they don't need to reach into private state — per CLAUDE.md
        testing policy.
        """
        return self._panel_visible

    @property
    def esc_esc_pending(self) -> bool:
        """Public read of the Esc-Esc first-tap state (#1546).

        ``True`` after one *truly clean* Esc, while a second clean Esc within
        ``_ESC_ESC_WINDOW_S`` would open the /rewind picker; ``False`` once the
        window lapses, the double-tap fires, or any Esc dismisses something.
        Exposed so tests read state through the public surface.
        """
        return self._last_clean_esc_ts != 0.0

    @property
    def rewind_menu_open(self) -> bool:
        """Public read of the /rewind picker visibility state (ADR-0038 1f).

        ``True`` while the inline RewindMenuWidget is mounted, ``False``
        otherwise. Mutate via ``_open_rewind_menu()`` / ``_dismiss_rewind_menu()``
        — direct assignment to ``_rewind_menu`` is not the public contract.
        Exposed for tests so they read state through the public surface.
        """
        return self._rewind_menu is not None

    @property
    def outbox_router(self) -> "OutboxRouter | None":
        """The active ``OutboxRouter`` instance, or ``None`` when no registry.

        Set to a live router by ``_outbox_loop`` when a registry is
        attached; reset to ``None`` when the loop exits. Tests use this
        to assert on router presence / absence without reaching into the
        private slot — per CLAUDE.md testing policy.
        """
        return self._outbox_router

    @property
    def current_stream_id(self) -> "str | None":
        """The msg_id of the most-recently started stream, or ``None``.

        Updated on every ``__stream_start__`` and cleared to ``None``
        when the matching ``__stream_end__`` arrives. Tests that need to
        assert on the global stream pointer use this property rather than
        ``_current_stream_id`` directly.
        """
        return self._current_stream_id

    @property
    def agent_name(self) -> "str | None":
        """The current agent name string shown in the header.

        Updated by ``_on_attach_request`` when ``/attach`` changes the
        active agent. Tests assert on this rather than ``_agent_name``.
        """
        return self._agent_name

    def skill_exec_snapshot(self) -> "dict[str, dict]":
        """Return a shallow copy of the in-flight skill-exec registry.

        ``_skill_exec`` maps run_id → metadata dict for every skill that
        is currently executing.  Tests call this to assert on membership
        without holding a live reference to the internal dict — per
        CLAUDE.md testing policy.
        """
        return dict(self._skill_exec)

    # ── composition ───────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield ReynHeader(
            agent_name=self._agent_name,
            model=self._model,
            id="header",
        )
        project_root: Path | None = None
        if self._agent_registry is not None:
            try:
                project_root = self._agent_registry._project_root
            except Exception:
                pass
        with Horizontal(id="content"):
            yield ConversationView(id="conversation")
            yield RightPanel(
                registry=self._agent_registry,
                project_root=project_root,
                id="right_panel",
            )
        yield InputBar(id="inputbar")

    def on_mount(self) -> None:
        """Wire up after DOM is ready."""
        # Load slash commands from registry into the inline picker
        from reyn.chat.slash import REGISTRY as SLASH_REGISTRY

        inputbar = self.query_one("#inputbar", InputBar)
        inputbar.update_slash_commands(SLASH_REGISTRY.all_commands())

        inputbar.focus_input()

        # Restore persisted TUI prefs (= /cost-inline state etc.) from
        # ``<project_root>/.reyn/tui_prefs.json``. Defaults stay in-code
        # when the file is absent / malformed (= first launch, fresh
        # project). F10: previously /cost-inline reset to "off" on
        # every restart because no persistence existed.
        from reyn.chat.tui.prefs import load_tui_prefs
        prefs = load_tui_prefs(self._project_root_path())
        self._cost_inline_enabled = bool(prefs.get("cost_inline", False))

        # issue #254 Phase 1: declare ourselves as the intervention listener
        # for the attached session. Without this, ``ChatSession`` constructed
        # with ``enforce_listener_presence=True`` would short-circuit
        # ``_dispatch_intervention`` and prompts would never reach the TUI.
        # ``_get_session()`` may return None during early lifecycle (no
        # session yet attached); the answer-input path will re-register
        # on session attach if that becomes a real case.
        session = self._get_session()
        if session is not None:
            session.register_intervention_listener("tui")

        # Optional ASCII banner (neofetch style): gradient logo left, agent
        # info right. Off by default — daily use should focus the input bar
        # instantly. Opt-in via `reyn chat --banner` (see cli/commands/chat.py).
        if self._banner:
            conv = self.query_one("#conversation", ConversationView)
            from rich.text import Text
            _BANNER = [
                "██████╗ ███████╗██╗   ██╗███╗   ██╗",
                "██╔══██╗██╔════╝╚██╗ ██╔╝████╗  ██║",
                "██████╔╝█████╗   ╚████╔╝ ██╔██╗ ██║",
                "██╔══██╗██╔══╝    ╚██╔╝  ██║╚██╗██║",
                "██║  ██║███████╗   ██║   ██║ ╚████║",
                "╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═══╝",
            ]
            _INFO = [
                None,
                ("agent", self._agent_name or "—"),
                ("model", self._model or "—"),
                None,
                None,
                None,
            ]
            n = len(_BANNER)
            for i, (line, info) in enumerate(zip(_BANNER, _INFO)):
                t = i / (n - 1)
                r, g, b = int(200 - 126 * t), int(85 - 59 * t), int(61 - 49 * t)
                rt = Text()
                rt.append(line, style=f"#{r:02x}{g:02x}{b:02x}")
                if info:
                    key, val = info
                    rt.append("    ")
                    rt.append(f"{key}  ", style=f"dim {_TEXT_DIM}")
                    rt.append(val, style=_TEXT_BRIGHT)
                conv._write_log(rt)
            conv._write_log(Text("  Gives you the reins.", style=f"dim {_TEXT_DIM}"))
            conv._write_log(Text("─" * 38, style=_BORDER_DIM))

        # ``--no-restore`` was previously surfaced only via a stderr print
        # from the CLI entry point, which the TUI overlay completely hides.
        # Echo the same reassurance into the conversation pane so the
        # operator can confirm the flag took effect even after the TUI is
        # already in the foreground. Amber styling matches other
        # advisory-but-non-fatal lines (= header `[N pending]` badge).
        if self._no_restore:
            conv = self.query_one("#conversation", ConversationView)
            from rich.text import Text as _RichText
            conv._write_log(_RichText(
                "⚠ --no-restore: in-flight skill state was NOT loaded this run. "
                "Restart without --no-restore to resume.",
                style="#d4a017",  # palette-candidate: advisory-warning amber (no-restore notice)
            ))

        # Start outbox subscription if registry is available
        if self._agent_registry is not None:
            self._outbox_task = asyncio.create_task(self._outbox_loop())

        # Periodic status line refresh (budget counters update between messages)
        self.set_interval(1.0, self._periodic_status_refresh)

    # ── outbox subscription (phases 3+) ────────────────────────────────────────

    async def _outbox_loop(self) -> None:
        """Drain registry.repl_outbox via :class:`OutboxRouter`.

        The full per-kind dispatch table + handlers live in ``app_outbox``;
        this method just constructs the router. Keeps ``app.py`` focused on
        composition / lifecycle.
        """
        if self._agent_registry is None:
            return
        from .app_outbox import OutboxRouter
        self._outbox_router = OutboxRouter(self)
        try:
            await self._outbox_router.run()
        finally:
            self._outbox_router = None

    def _mount_intervention(
        self,
        conv: ConversationView,
        text: str,
        iv_id: str,
        choices: list[tuple[str, str] | dict] | None = None,
        queued_extra: int = 0,
        detail: str | None = None,
        source_agent: str | None = None,
    ) -> None:
        """Mount an InterventionWidget inline in the conversation view.

        When `choices` is provided (from meta["choices"]), chip buttons are
        rendered. The user's answer (chip label or free text) is routed via
        session._maybe_answer_oldest_intervention — which matches hotkeys /
        choice labels against the pending intervention.

        ``choices`` accepts both legacy ``(label, id)`` 2-tuples and the
        richer ``{"label", "id", "hotkey", "default"}`` dict shape — the
        widget normalises both forms internally.

        ``queued_extra`` surfaces the number of additional pending
        interventions waiting behind the head one (caller-computed) so the
        widget can render a persistent ``+N more pending`` badge.
        """
        async def _callback(answer: str) -> None:
            session = self._get_session()
            if session is not None:
                await session._maybe_answer_oldest_intervention(answer)

        conv.mount_intervention(
            question=text,
            choices=choices,
            answer_callback=_callback,
            iv_id=iv_id,
            queued_extra=queued_extra,
            detail=detail,
            source_agent=source_agent,
        )

    async def on_intervention_widget_skip_rest(
        self,
        event: InterventionWidget.SkipRest,
    ) -> None:
        """Cancel every queued non-head intervention + emit one breadcrumb.

        Wave-13 T2-4 / audit finding B#5.  When the user clicks the
        "skip rest (N pending)" chip on the head InterventionWidget, we
        cancel all interventions behind the head one (= all queue entries
        except the one whose id matches ``event.iv_id``).  The head
        intervention stays active so the user can still answer it.

        A single dim breadcrumb is written to the conv log:
            ✗ N interventions skipped (see /pending list)
        This is surgical — it does NOT Ctrl+C, does NOT cancel running
        skills, and does NOT remove the head widget.
        """
        event.stop()
        session = self._get_session()
        interventions = (
            getattr(session, "_interventions", None) if session is not None else None
        )
        cancelled = 0
        if interventions is not None:
            for iv in list(interventions.list_active()):
                if iv.id == event.iv_id:
                    # Head intervention — leave it alive for the user.
                    continue
                if interventions.cancel(iv.id):
                    cancelled += 1
        if cancelled:
            from rich.text import Text as _RichText
            label = "intervention" if cancelled == 1 else "interventions"
            breadcrumb = _RichText()
            breadcrumb.append(
                f"  ✗ {cancelled} {label} skipped (see /pending list)",
                style=f"dim {_RED_MUTED}",
            )
            try:
                conv = self.query_one("#conversation", ConversationView)
                conv._write_log(breadcrumb)
            except Exception:
                pass
        # Remove the skip chip from the head widget so the button
        # doesn't linger after there are no more queued IVs to skip.
        try:
            for w in self.query(InterventionWidget):
                try:
                    skip_btn = w.query_one("#chip__skip_rest")
                    skip_btn.remove()
                except Exception:
                    pass
        except Exception:
            pass

    def set_title_state(self, state: str | None) -> None:
        """Set the terminal window title to reflect the agent's state.

        ``state`` is a short verb / noun like ``"awaiting answer"``,
        ``"error"``, or ``None`` for idle. The title shown in the tab
        bar becomes ``"reyn — <state>"`` (or just ``"reyn"`` for idle)
        so a user with reyn in a background terminal tab can see the
        state without focusing the window. Wraps ``self.title`` to
        contain the formatting in one place and let tests assert on a
        normalised state token.
        """
        try:
            self.title = f"reyn — {state}" if state else "reyn"
        except Exception:
            pass

    def alert(self) -> None:
        """Fire the terminal BEL for "you need to come back" events.

        Wraps ``self.bell()`` so call sites don't need to know whether
        the platform supports it (the call is a no-op on terminals that
        ignore ``\\a``). The helper exists so future ``/quiet`` opt-out
        can short-circuit one place rather than every hook point.
        """
        try:
            self.bell()
        except Exception:
            pass

    def _maybe_refresh_status(self, header: ReynHeader | None = None) -> None:
        """Fetch budget snapshot and update the header.

        Can be called with a pre-fetched header widget (from outbox loop)
        or None (will query the DOM — safe from the main thread).
        """
        if header is None:
            try:
                header = self.query_one("#header", ReynHeader)
            except Exception:
                return
        if self._budget_tracker is None:
            # Still update agent name + model even without budget
            header.refresh_status(
                agent_name=self._agent_name,
                model=self._model,
            )
            return
        try:
            snap = self._budget_tracker.snapshot()
            # Caps live on the BudgetConfig object referenced by
            # ``snap["config"]`` (= ``cfg.daily_tokens.hard_limit`` /
            # ``cfg.daily_cost_usd.hard_limit``). The previous wiring
            # looked them up as flat ``daily_tokens_cap`` /
            # ``daily_cost_usd_cap`` keys on the snapshot dict, which
            # don't exist — so ``snap.get(...)`` always returned None,
            # and the header's ``[N tok / <cap>]`` / ``$N / $<cap>``
            # suffixes never appeared even when the user had configured
            # ``cost.daily_cost_usd.hard_limit`` in ``reyn.local.yaml``.
            cfg = snap.get("config")
            tok_cap: int | None = None
            cost_cap: float | None = None
            if cfg is not None:
                tok_cap_cfg = getattr(cfg, "daily_tokens", None)
                cost_cap_cfg = getattr(cfg, "daily_cost_usd", None)
                tok_hard = (
                    getattr(tok_cap_cfg, "hard_limit", None)
                    if tok_cap_cfg else None
                )
                cost_hard = (
                    getattr(cost_cap_cfg, "hard_limit", None)
                    if cost_cap_cfg else None
                )
                if tok_hard and tok_hard > 0:
                    tok_cap = int(tok_hard)
                if cost_hard and cost_hard > 0:
                    cost_cap = float(cost_hard)
            header.refresh_status(
                agent_name=self._agent_name,
                model=self._model,
                tokens_today=snap.get("daily_tokens", 0),
                tokens_cap=tok_cap,
                cost_usd=snap.get("daily_cost_usd", 0.0),
                cost_cap=cost_cap,
                stalled_count=self._poll_stalled_count(),
            )
        except Exception:
            pass

    def _poll_stalled_count(self) -> int:
        """Issue #277 — count of stalled / cross-channel pending ops.

        Read from the attached session's intervention registry (= the
        Phase 1 ``InterventionRegistry.stalled_count`` API). Returns 0
        when no session is attached or the registry is unavailable
        (= the header badge will be hidden, leaving the cold-default
        layout unchanged).
        """
        try:
            session = self._get_session()
            if session is None:
                return 0
            registry = getattr(session, "_interventions", None)
            if registry is None:
                return 0
            return int(registry.stalled_count())
        except Exception:
            return 0

    def _periodic_status_refresh(self) -> None:
        """Called every 1s by set_interval to keep status line current."""
        self._maybe_refresh_status()

    def _update_skill_exec(self, msg) -> None:
        """Parse a trace OutboxMessage with skill_name in meta and update _skill_exec."""
        import time as _time
        run_id = msg.meta.get("run_id", "")
        if not run_id:
            return
        skill_name = msg.meta.get("skill_name", "")
        text = msg.text or ""
        existing = self._skill_exec.get(run_id)
        if existing is None:
            existing = {
                "skill_name": skill_name,
                "agent_name": self._agent_name,
                "start_time": _time.monotonic(),
                "phase": "",
                "phase_visits": 0,
                # Snapshot of the user message that triggered this run.
                # Captured ONCE, on the first trace, so a long-tail skill
                # that's still running when the user types another
                # message keeps the message that actually started it.
                "triggered_by": self._latest_user_message,
                # Stamped by ChatEventForwarder._enqueue (PR #198) on
                # every sub-skill trace. Empty when this is a root
                # skill. Used by the conv pane (issue #210 child row
                # indent) and the right-panel agents tab (RichTree
                # nesting under parent).
                "parent_run_id": msg.meta.get("parent_run_id", ""),
            }
            self._skill_exec[run_id] = existing
            # Mirror to the persistent map so recent_skill (= post-skill_done)
            # can still show what triggered the run. Soft-cap to avoid
            # unbounded growth on long sessions; oldest entries drop first.
            if self._latest_user_message:
                self._run_id_to_user_message[run_id] = self._latest_user_message
                if len(self._run_id_to_user_message) > 200:
                    # Pop the oldest insertion (dict preserves order).
                    self._run_id_to_user_message.pop(
                        next(iter(self._run_id_to_user_message)),
                    )
            # AsyncStackPanel wiring: this branch is the "task spawned"
            # signal for the TUI (= first trace for this run_id, the
            # ``[task_spawned] kind=skill`` notification the LLM sees
            # in its context). Add the row to the bottom-docked
            # running-tasks overview. The corresponding remove fires
            # in ``_handle_trace_for_skill_row`` on the
            # ``"skill done: …"`` trace (= the matching
            # ``[task_completed]`` boundary).
            try:
                conv = self.query_one("#conversation", ConversationView)
                conv.add_async_task(run_id, skill_name or "skill")
            except Exception:
                pass
        # Text pattern: "phase started: <phase_name>"
        if text.startswith("phase started: "):
            phase = text[len("phase started: "):].strip()
            existing["phase"] = phase
            existing["phase_visits"] = existing.get("phase_visits", 0) + 1
        # Text pattern: "detail: plan N/M" (= ChatEventForwarder one-shot
        # plan-step badge emit, see forwarder.on_phase_started). Capture
        # the N/M values into _skill_exec so the right-panel agents tab
        # can render a [plan N/M] badge alongside the running skill row
        # — same data wave-7 PR #418 routed into SkillActivityRow's
        # persistent slot for the conv pane. C-F2 from the wave-7
        # Topic C exploration.
        if text.startswith("detail: plan ") and "/" in text:
            badge = text[len("detail: "):].strip()
            tail = badge[len("plan "):]
            if "/" in tail:
                head, _, rest = tail.partition("/")
                # rest may have trailing words; take the leading int run.
                n_total_str = rest.split()[0] if rest else ""
                try:
                    existing["plan_n_done"] = int(head)
                    existing["plan_n_total"] = int(n_total_str)
                except (ValueError, IndexError):
                    pass

    def _push_exec_state(self) -> None:
        """Forward current _skill_exec snapshot to RightPanel for live display."""
        try:
            panel = self.query_one("#right_panel", RightPanel)
            panel.update_exec_state(self._skill_exec)
        except Exception:
            pass

    def _handle_trace_for_skill_row(self, conv: ConversationView, msg) -> None:
        """Mount or update a SkillActivityRow from a `kind="trace"` message.

        Recognised text patterns from ChatEventForwarder:
          - "phase started: <phase_name>" → start row (if missing) + set_phase
          - "<phase> → <next> ..."         → phase_completed transition. The
            outgoing phase's LLM call may take 10-30 s before the next
            ``phase started`` arrives; without this branch the row would
            stay on the OLD phase name the whole time, making the skill
            look stuck. Show ``<phase> → <next>`` so the user can see the
            handoff is in flight.
          - "skill done: <status>"         → finish row (FP-0011/FP-0012)
        """
        run_id = msg.meta.get("run_id", "")
        skill_name = msg.meta.get("skill_name", "") or ""
        if not run_id:
            return
        text = msg.text or ""
        if text.startswith("phase started: "):
            phase = text[len("phase started: "):].strip()
            # Lazy-create the row if first trace
            conv.start_skill_row(
                run_id,
                skill_name,
                parent_run_id=msg.meta.get("parent_run_id", ""),
            )
            # Track phase visit count from existing exec state
            existing = self._skill_exec.get(run_id) or {}
            visit = int(existing.get("phase_visits", 0)) + 1
            conv.update_skill_phase(run_id, phase, visit=visit)
            self._last_focal_tab = "agents"
        elif text.startswith("detail: "):
            # In-phase detail (llm call / act batch / etc). Append a dim
            # ``⤷ <detail>`` segment to the row so the user sees the
            # skill is actively working rather than wondering if it's
            # stuck on the phase name. An empty detail (``"detail: "``)
            # clears the segment — that's how the forwarder signals
            # "llm call finished; we're between events".
            detail = text[len("detail: "):]
            conv.update_skill_detail(run_id, detail)
        elif " → " in text and not text.startswith("skill done: "):
            # phase_completed trace: ``<phase> → <next>(  (confidence=X))?``
            # Strip the optional confidence suffix and display the bare
            # transition so the row reflects "old phase has handed off to
            # next" until the next ``phase started`` arrives. The visit
            # count is left at the existing exec state (= the just-
            # finished phase's visit) so the badge doesn't bump too
            # early.
            transition = text.split("  (confidence=", 1)[0].strip()
            existing = self._skill_exec.get(run_id) or {}
            # If no row exists yet (edge case: a malformed forwarder
            # delivered phase_completed before phase_started), lazy-mount
            # to keep behaviour robust.
            conv.start_skill_row(
                run_id,
                skill_name,
                parent_run_id=msg.meta.get("parent_run_id", ""),
            )
            visit = int(existing.get("phase_visits", 1)) or 1
            conv.update_skill_phase(run_id, transition, visit=visit)
            self._last_focal_tab = "agents"
        elif text.startswith("skill done: "):
            # FP-0011: skill_narrator removed → skill_done outbox kind gone.
            # ChatEventForwarder now forwards workflow_finished / workflow_aborted
            # as "skill done: <status>" so the row can stop spinning here.
            payload = text[len("skill done: "):].strip()
            # C-F2 (wave-8): forwarder may append the abort reason as
            # ``"aborted: <reason>"`` — split on the first colon so we
            # extract both the bare status (= "finished" / "aborted")
            # and the human-readable reason for the ``✗`` finish line.
            # Bare ``"aborted"`` (= no reason) falls through to the
            # legacy phase-count fallback below.
            abort_reason = ""
            if ": " in payload:
                status, abort_reason = payload.split(": ", 1)
                status = status.strip()
                abort_reason = abort_reason.strip()
            else:
                status = payload
            # Wave-3 SK2: pass the phase-visit count as ``reason`` so
            # ``SkillActivityRow._build_finished`` renders the
            # ``· N phase(s)`` suffix. C-F2 (wave-8): when the forwarder
            # supplied an explicit abort reason, that wins over the
            # phase count — the user wants to see "timeout" or
            # "budget exceeded", not just "2 phases".
            if abort_reason:
                _reason = abort_reason
            else:
                _visits = int(
                    (self._skill_exec.get(run_id) or {}).get("phase_visits", 0)
                )
                _reason = (
                    f"{_visits} phase{'s' if _visits != 1 else ''}"
                    if _visits > 0 else ""
                )
            # Defensive cleanup: each action is wrapped independently so
            # one widget's failure doesn't block subsequent cleanup steps.
            # Previously the block was fully sequential — if finish_skill_row
            # raised (e.g. for skills that failed before any phase_started
            # trace fired, leaving no SkillActivityRow for the run_id),
            # the subsequent remove_async_task call was skipped and the
            # AsyncStackPanel row stayed mounted with its elapsed counter
            # ticking forever (user-observed: mcp_search WorkflowAbortedError).
            # Same defensive pattern as skill_runner.py:733-738 fallback emit.
            try:
                conv.finish_skill_row(
                    run_id, success=(status == "finished"), reason=_reason,
                )
            except Exception:
                pass
            # Out-of-band: an aborted skill is an attention case — flash the
            # title to "error" and ring the bell so a user with reyn in a
            # background tab sees something happened. Successful finishes
            # are silent here (the next agent reply / cost suffix is the
            # in-pane signal). The title resets on the next user submit.
            if status != "finished":
                try:
                    self.set_title_state("error")
                except Exception:
                    pass
                try:
                    self.alert()
                except Exception:
                    pass
            # Wave-3 SK1: previously a second ``conv._write_log`` line
            # ``✓ skill_name: finished (Xs)`` was emitted here, but
            # ``SkillActivityRow._build_finished`` (= the row the
            # ``finish_skill_row`` call above transitions) already
            # renders ``✓ skill#abcd  · Ns  · Ctrl+B → agents`` /
            # ``✗ skill#abcd  · failed: …`` in-pane. The dual marker
            # cluttered the conv log with redundant lines. Drop the
            # ``_write_log`` block; book-keeping (= ``_skill_exec``
            # pop + ``_push_exec_state`` + ``_last_focal_tab``)
            # stays.
            try:
                self._skill_exec.pop(run_id, None)
            except Exception:
                pass
            try:
                self._push_exec_state()
            except Exception:
                pass
            self._last_focal_tab = "agents"
            # AsyncStackPanel wiring: the ``[task_completed]`` boundary
            # for the attached agent's task — drop the row from the
            # bottom-docked overview. Mirrors the ``add_async_task``
            # call in ``_update_skill_exec``'s first-trace branch.
            # Wave-13 T2-2: pass terminal="aborted" when the skill did
            # not finish cleanly so the bottom strip briefly flashes
            # the red ✗ shape before unmounting (= audit finding A#6).
            _async_terminal = "ok" if status == "finished" else "aborted"
            try:
                conv.remove_async_task(run_id, terminal=_async_terminal)
            except Exception:
                pass
            # Wave-9 D-F11: release the input-bar lock when the last
            # tracked skill finishes. Sub-skills popping during a
            # nested run keep the lock held — only the empty state
            # signals "turn is fully done, safe to accept a new
            # submit". ``_on_stream_end`` is the other unlock path
            # (covers streaming turns that don't necessarily route
            # through ``_skill_exec``); both are idempotent so calling
            # twice in a turn is harmless.
            if not self._skill_exec:
                try:
                    self.query_one("#inputbar", InputBar).set_in_flight(False)
                except Exception:
                    pass

    def _maybe_render_cost_suffix(
        self, conv: ConversationView, *, attempt: int = 0,
    ) -> None:
        """A4 — emit a dim per-turn cost suffix line when /cost-inline is on.

        Wave-6 ST3: defer the write while any skill / plan from this
        turn is still running. The agent-reply outbox fires when the
        LLM text reply lands, but post-reply phases (plan aggregator,
        chat_router final follow-up, compaction) continue to spend
        tokens. Writing at agent-reply time pinned a snapshot 5-10 s
        earlier than the user's perceived "turn finished" moment, so
        the ``⌁ Nt │ $X.XXXX │ Ys`` line under-reported the real total.

        Strategy: peek at ``self._skill_exec`` (= map of run_id →
        live skill snapshot maintained by ``_handle_trace_for_skill_row``).
        Non-empty = at least one skill row is still spinning. Defer
        by 1 s and re-attempt, up to ``_COST_SUFFIX_DEFER_MAX_ATTEMPTS``
        times. If the cap is reached (= a skill is genuinely long-
        running and the cost suffix would otherwise never fire) write
        with what we have to preserve the previous always-visible UX.
        """
        if not self._cost_inline_enabled:
            return
        if self._budget_tracker is None:
            return
        if attempt < _COST_SUFFIX_DEFER_MAX_ATTEMPTS and self._skill_exec:
            self.set_timer(
                _COST_SUFFIX_DEFER_INTERVAL_S,
                lambda: self._maybe_render_cost_suffix(
                    conv, attempt=attempt + 1,
                ),
            )
            return
        # Wave-7 C-F6: when the cap fires while a skill is still running,
        # the snapshot under-reports the eventual total. Mark the line
        # partial so the user knows to look for a follow-up emit.
        partial = bool(self._skill_exec) and attempt >= _COST_SUFFIX_DEFER_MAX_ATTEMPTS
        try:
            snap = self._budget_tracker.snapshot()
            cost_now = float(snap.get("daily_cost_usd", 0.0))
            tokens_now = int(snap.get("daily_tokens", 0))
        except Exception:
            return
        delta_cost = max(0.0, cost_now - self._turn_start_cost_usd)
        delta_tokens = max(0, tokens_now - self._turn_start_tokens)
        elapsed = max(0.0, _now_monotonic() - self._turn_start_time)
        # Suppress when the delta is exactly zero (no model call this turn)
        if delta_cost == 0.0 and delta_tokens == 0:
            return
        try:
            conv.render_cost_suffix(
                delta_tokens, delta_cost, elapsed, partial=partial,
            )
        except Exception:
            pass

    def _record_turn_start(self) -> None:
        """Capture cost / token / time snapshot at the start of a user turn."""
        self._turn_start_time = _now_monotonic()
        if self._budget_tracker is not None:
            try:
                snap = self._budget_tracker.snapshot()
                self._turn_start_cost_usd = float(snap.get("daily_cost_usd", 0.0))
                self._turn_start_tokens = int(snap.get("daily_tokens", 0))
            except Exception:
                self._turn_start_cost_usd = 0.0
                self._turn_start_tokens = 0
        else:
            self._turn_start_cost_usd = 0.0
            self._turn_start_tokens = 0

    # ── message handlers from widgets ─────────────────────────────────────────

    async def on_input_bar_user_submitted(self, msg: InputBar.UserSubmitted) -> None:
        """User hit Enter — dispatch to session or slash registry."""
        text = msg.text.strip()
        if not text:
            return

        conv = self.query_one("#conversation", ConversationView)
        # B1: render with grouped header
        conv.render_user_message(text)
        # A4: snapshot cost/tokens/time at turn start
        self._record_turn_start()
        # Out-of-band: a fresh user submit clears any prior "awaiting
        # answer" / "error" title flag — the user is back at the
        # keyboard, so the attention signal has done its job.
        self.set_title_state(None)
        # Remember this message so the next skill run that fires can
        # tag itself with "triggered_by". Cleared / overwritten on each
        # subsequent submit, so a long-tail skill that finishes after
        # the user has typed again still keeps the message that
        # actually started it (= we capture eagerly on first trace).
        self._latest_user_message = text

        # Get the attached session
        session = self._get_session()

        if text.startswith("/"):
            # ``/quit`` / ``/exit`` previously had a hard-coded intercept
            # here that short-circuited to ``action_quit_tui`` before the
            # registry dispatch. Wave-2 P3 moved them into the registry
            # (``reyn.chat.slash.quit``) so they surface in the palette
            # + ``/help``; the registry handler emits ``__quit__`` and
            # ``app_outbox._on_quit`` runs the same shutdown. Removing
            # the intercept keeps a single source of truth for the slash
            # dispatch path.
            # Dispatch to session._maybe_handle_slash (which uses registry)
            try:
                if session is not None:
                    await session._maybe_handle_slash(text)
                else:
                    # No session — still try the registry
                    await self._dispatch_slash_no_session(text, conv)
            finally:
                # Wave-9 D-F11: slash commands are synchronous from the
                # user's POV — once ``_maybe_handle_slash`` returns the
                # turn is over, so release the in-flight lock the
                # ``InputBar._submit`` set. Without this, a slash submit
                # would leave the input bar locked until the next
                # stream_end (which may never come for a pure slash
                # turn).
                # B5: also refresh the hint so the pending-image badge
                # updates after ``/image`` queues an image (or any other
                # slash that mutates the queue). refresh_hint() is cheap
                # (one property read + Label.update) and idempotent.
                try:
                    bar = self.query_one("#inputbar", InputBar)
                    bar.set_in_flight(False)
                    bar.refresh_hint()
                except Exception:
                    pass
        else:
            if session is not None:
                # Optimistic sticky: the skill_runner emits its first
                # ``status="thinking…"`` ~0.5s after submit (= when the
                # LLM request is about to leave). Without this line the
                # sticky bar is blank in that gap, which is most
                # noticeable right after a Ctrl+C cancel (the user just
                # cleared the previous spinner and wants instant
                # confirmation the new turn is starting). When the
                # natural ``thinking…`` message arrives, it overwrites
                # with identical text — no flicker.
                conv.start_thinking()
                await session.submit_user_text(text)
            else:
                from rich.text import Text as RichText
                t = RichText()
                t.append("✗ ", style="bold red")
                t.append("no session attached", style="bold red")
                conv._write_log(t)

    async def _dispatch_slash_no_session(self, text: str, conv: ConversationView) -> None:
        """Fallback: show error when no session attached for slash commands."""
        from rich.text import Text as RichText
        t = RichText()
        t.append("✗ ", style="bold red")
        t.append("no agent session attached; use 'reyn chat' with an agent configured")
        conv._write_log(t)

    def on_input_bar_clear_conversation(self, msg: InputBar.ClearConversation) -> None:
        """Ctrl+L — clear the conversation pane."""
        self.action_clear_conversation()

    def on_input_bar_quit_requested(self, msg: InputBar.QuitRequested) -> None:
        """Ctrl+D — quit."""
        self.call_later(self.action_quit_tui)

    def on_input_bar_cancel_in_flight(self, msg: InputBar.CancelInFlight) -> None:
        """Ctrl+C — cancel the in-flight model call."""
        self.action_cancel_inflight()

    # ── actions ───────────────────────────────────────────────────────────────

    async def action_quit_tui(self) -> None:
        """Graceful shutdown: cancel outbox task then exit."""
        if self._outbox_task and not self._outbox_task.done():
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
        if self._agent_registry is not None:
            try:
                await self._agent_registry.shutdown()
            except Exception:
                pass
        self.exit()

    def action_clear_conversation(self) -> None:
        conv = self.query_one("#conversation", ConversationView)
        conv.clear()

    def action_screenshot(self) -> None:
        """Ctrl+\\ — save an SVG screenshot, open it, and log the path."""
        filename = self.save_screenshot()
        try:
            import subprocess
            import sys
            from pathlib import Path

            from rich.text import Text
            abs_path = Path(filename).resolve()
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(abs_path)])
            elif sys.platform == "win32":
                import os
                os.startfile(str(abs_path))
            else:
                subprocess.Popen(["xdg-open", str(abs_path)])
            t = Text()
            t.append("screenshot → ", style="dim")
            t.append(str(abs_path), style="bold")
            self.query_one("#conversation", ConversationView)._write_log(t)
        except Exception:
            pass

    def action_cancel_inflight(self) -> None:
        """Cancel the in-flight skill/plan/model call on the attached session.

        Visibility: unmounts the inline thinking spinner AND clears the
        sticky ``⟳ thinking…`` indicator. Also writes a one-line summary
        to the conv pane reporting how many skills / plans were actually
        cancelled. Without this summary the user couldn't tell whether
        Ctrl+C did anything (= "did it really cancel, or was nothing in
        flight?").

        Ctrl+C is hierarchical: if the slash picker is open, dismiss it
        first (matches Esc, satisfies the "abort the visible thing"
        intuition) and do NOT also touch in-flight state. Without a
        picker visible, fall through to the cancel path below.
        """
        try:
            input_bar = self.query_one("#inputbar", InputBar)
        except Exception:
            input_bar = None
        if input_bar is not None and input_bar.dismiss_slash_prefix():
            return

        try:
            conv = self.query_one("#conversation", ConversationView)
        except Exception:
            conv = None
        if conv is not None:
            conv.stop_thinking()
            conv.hide_status()
        # Wave-9 D-F11: Ctrl+C unconditionally releases the in-flight
        # lock so the user can immediately submit a new prompt. Even if
        # no skills were actually cancelled (= the lock was set
        # spuriously, or a stream_end was missed), Ctrl+C is the
        # documented escape hatch from a stuck state.
        if input_bar is not None:
            input_bar.set_in_flight(False)

        session = self._get_session()
        # Wave-6 IV2: pending user interventions are "in-flight" from
        # the user's POV. Without this branch, Ctrl+C while a permission
        # prompt was on screen reported ``nothing in-flight to cancel``
        # and the modal stayed up indefinitely. Handle iv cancellation
        # BEFORE the session-None check because the visible widget
        # removal must fire even when the session has detached (= rare
        # but the conv pane still shows the stale modal otherwise).
        cancelled_interventions = 0
        interventions = (
            getattr(session, "_interventions", None) if session is not None else None
        )
        if interventions is not None:
            for iv in list(interventions.list_active()):
                if interventions.cancel(iv.id):
                    cancelled_interventions += 1
        intervention_widgets_dismissed = 0
        if conv is not None:
            from .widgets.intervention import InterventionWidget
            for widget in list(conv.query(InterventionWidget)):
                try:
                    widget.remove()
                    intervention_widgets_dismissed += 1
                    if interventions is None:
                        # No registry to cancel against — still count
                        # the visible dismissal so the summary line
                        # reflects what the user just saw disappear.
                        cancelled_interventions += 1
                except Exception:
                    pass
        # Wave-9 E-F3: restore focus to the InputBar after dismissing
        # one or more intervention widgets. ``InterventionWidget._submit``
        # and ``_on_intervention_resolved`` both mirror this restore on
        # their respective dismiss paths; without it here the Textual
        # focus-walker picks the next focusable widget in DOM order on
        # ``widget.remove()``, which on a Ctrl+C cancel typically lands
        # on a SkillActivityRow or a right-panel element — the user's
        # next keystroke goes nowhere and they must manually Tab back.
        if intervention_widgets_dismissed and input_bar is not None:
            input_bar.focus_input()
        if session is None:
            if cancelled_interventions:
                self._voice_status(
                    f"✗ cancelled {cancelled_interventions} "
                    f"intervention{'s' if cancelled_interventions != 1 else ''}",
                    style=f"bold {_RED_MUTED}",
                )
            else:
                self._voice_status(
                    "(nothing to cancel — no session attached)",
                    style=f"dim {_RED_MUTED}",
                )
            return

        # Issue #276 Phase B: remote (``--connect``) mode delegates the
        # cancel to the server. The ``_WSSessionProxy`` exposes
        # ``cancel_inflight()`` which fires a WS frame; the server-side
        # endpoint iterates the real ``session.running_skills`` /
        # ``running_plans`` and emits a ``status`` outbox back with the
        # "✗ cancelled N skill + M plan" summary. The local proxy's
        # ``running_skills`` / ``running_plans`` dicts are always empty
        # (state lives server-side), so the legacy iteration below
        # would be a no-op and we'd report "(nothing in-flight to
        # cancel)" even when the remote agent is mid-call. Detect the
        # proxy by the registry holding a ``_ws`` attribute — only the
        # WS client (``_WSRegistry``) sets that; local AgentRegistry
        # doesn't.
        is_remote_proxy = getattr(self._agent_registry, "_ws", None) is not None
        remote_cancel = getattr(session, "cancel_inflight", None)
        if is_remote_proxy and callable(remote_cancel):
            asyncio.create_task(remote_cancel())
            # Optimistic local feedback — the authoritative summary
            # arrives as a ``status`` outbox frame from the server.
            self._voice_status(
                "cancel sent to remote — awaiting confirmation",
                style=f"dim {_RED_MUTED}",
            )
            # Seal any locally-tracked skill-activity rows so their
            # spinners stop immediately (the remote will send
            # ``trace`` workflow_aborted events shortly but the
            # visible spinner shouldn't keep ticking in the meantime).
            if conv is not None and self._skill_exec:
                for run_id in list(self._skill_exec.keys()):
                    try:
                        # C-F5 (wave-8): aborted=True → ⊘ glyph (not ✗)
                        # so user-initiated cancel reads visually distinct
                        # from a system failure.
                        conv.finish_skill_row(
                            run_id,
                            success=False,
                            reason="cancelled (remote)",
                            aborted=True,
                        )
                    except Exception:
                        pass
                    # AsyncStackPanel: drop the row for this cancelled
                    # task — the remote cancel path tears down the
                    # task locally without waiting for a
                    # workflow_aborted trace, so the bottom-stack
                    # entry needs explicit removal here (= same
                    # rationale as the ``_skill_exec.pop`` directly
                    # below).
                    # Wave-13 T2-2: terminal="aborted" so the strip
                    # briefly flashes red before unmounting.
                    try:
                        conv.remove_async_task(run_id, terminal="aborted")
                    except Exception:
                        pass
                    self._skill_exec.pop(run_id, None)
                self._push_exec_state()
            # C-F1: seal in-flight ToolCallRow widgets too. Without this
            # sweep, any tool_call that was mid-run when Ctrl+C fires
            # leaves an orphan ``●`` spinner in scroll history with no
            # terminal glyph — the user sees a frozen running indicator.
            if conv is not None:
                conv.abort_tool_call_rows(reason="cancelled (remote)")
            return

        # #1468: single seam — delegate asyncio cancellation (turn flag +
        # skill tasks + plan tasks) to session.cancel_inflight(). The TUI
        # retains ownership of UI-layer cleanup (row sealing, stack panel
        # removal) which is TUI-specific and not appropriate to push into
        # the session model.
        #
        # Snapshot counts + plan_ids BEFORE firing the cancel so the UI
        # cleanup loops below have stable data (cancel is async; by the
        # time they run the dicts may have been mutated by the task runner).
        _running_skills_snap = {
            rid: t for rid, t in getattr(session, "running_skills", {}).items()
            if not t.done()
        }
        _running_plans_snap = {
            pid: t for pid, t in getattr(session, "running_plans", {}).items()
            if not t.done()
        }
        cancelled_skills = len(_running_skills_snap)
        cancelled_plans = len(_running_plans_snap)
        _cancel_fn = getattr(session, "cancel_inflight", None)
        if callable(_cancel_fn):
            asyncio.create_task(_cancel_fn())
        # UI-layer plan row cleanup: remove AsyncStackPanel rows for each
        # cancelled plan. Must happen here (TUI event loop), not in the
        # session. ``task.cancel()`` doesn't trigger the natural
        # ``source=plan_complete`` outbox path, so without this sweep the
        # bottom-strip rows persist after Ctrl+C.
        for plan_id in _running_plans_snap:
            if conv is not None:
                try:
                    # Wave-13 T2-2: terminal="interrupted" so the
                    # strip briefly flashes red before unmounting,
                    # distinguishing plan cancel from clean completion.
                    conv.remove_async_task(str(plan_id), terminal="interrupted")
                except Exception:
                    pass

        # Stop any SkillActivityRow spinners + clear the right-panel agents
        # tab. ``task.cancel()`` only stops the asyncio producer; no
        # ``workflow_finished`` / ``workflow_aborted`` trace is forwarded
        # after cancel, so without this loop the rows spin forever and the
        # agents tab shows ``● running`` indefinitely (~minutes observed).
        # Snapshot the keys first — ``finish_skill_row`` mutates _skill_rows.
        if conv is not None and self._skill_exec:
            for run_id in list(self._skill_exec.keys()):
                try:
                    # C-F5 (wave-8): aborted=True → ⊘ glyph (not ✗) for
                    # user-initiated cancel.
                    conv.finish_skill_row(
                        run_id,
                        success=False,
                        reason="cancelled",
                        aborted=True,
                    )
                except Exception:
                    pass
                # AsyncStackPanel: drop the cancelled task from the
                # bottom-stack overview. Mirrors the remote-cancel
                # branch above + the natural ``"skill done:"`` trace
                # path; needs explicit handling here because
                # ``task.cancel()`` doesn't produce a
                # workflow_aborted trace.
                # Wave-13 T2-2: terminal="aborted" → red flash before
                # unmount so user can tell user-cancelled from clean done.
                try:
                    conv.remove_async_task(run_id, terminal="aborted")
                except Exception:
                    pass
                self._skill_exec.pop(run_id, None)
            self._push_exec_state()

        # Seal any orphan streaming rows. The router cancel above stops the
        # producer but no `__stream_end__` is emitted, so the StreamingRow's
        # blinking cursor would otherwise persist forever. Snapshot first —
        # end_stream() mutates conv._stream_rows.
        cancelled_streams = 0
        if conv is not None:
            # Wave-9 F-F7: route through ``end_stream_cancelled`` so the
            # partial text gets the dim italic + "✗ cancelled (partial
            # reply):" header treatment instead of being committed with
            # the same Markdown styling as a finished reply (with only
            # a small dim suffix that's easy to miss when the partial
            # fills the viewport). The summary line emitted below still
            # reports ``cancelled_streams``.
            for msg_id in list(conv._stream_rows.keys()):
                try:
                    conv.end_stream_cancelled(msg_id)
                    cancelled_streams += 1
                except Exception:
                    pass

        # C-F1: seal any live ToolCallRow widgets so mid-run tool_calls
        # don't leave orphan ``●`` spinners in scroll history. The skill
        # task cancellation above doesn't emit a ``tool_failed`` event
        # for the in-flight call, so without this sweep the row never
        # reaches a terminal state. The row's frozen elapsed + ⊘ glyph
        # land in RichLog history (= matches the streaming-row pattern
        # just above).
        cancelled_tool_calls = 0
        if conv is not None:
            cancelled_tool_calls = conv.abort_tool_call_rows(reason="cancelled")

        if (
            cancelled_skills == 0
            and cancelled_plans == 0
            and cancelled_streams == 0
            and cancelled_interventions == 0
            and cancelled_tool_calls == 0
        ):
            # Suppress repeat lines: when the user mashes Ctrl+C on an idle
            # session, log a single "nothing-in-flight cancel" then
            # debounce for ``_IDLE_CANCEL_DEDUP_S`` so subsequent presses
            # don't litter the conv log with identical lines.
            now = _now_monotonic()
            if now - self._last_idle_cancel_ts > _IDLE_CANCEL_DEDUP_S:
                # ``✗`` prefix gives the line a glyph that matches the
                # other ``✗``-led cancellation / error feedback (= the
                # ``✗ cancelled N skill…`` summary below), and the
                # ``try /list`` tail tells the user how to verify
                # nothing's actually stuck (= the most common "wait,
                # did it really do nothing?" reaction to a no-op
                # cancel). The previous bare ``(nothing in-flight to
                # cancel)`` parenthetical had neither cue and read
                # like an unimportant aside.
                self._voice_status(
                    "✗ nothing in-flight to cancel — try /list to see active runs",
                    style="dim " + _TEXT_DIM,
                )
                self._last_idle_cancel_ts = now
            return
        parts: list[str] = []
        if cancelled_skills:
            parts.append(
                f"{cancelled_skills} skill{'s' if cancelled_skills != 1 else ''}"
            )
        if cancelled_plans:
            parts.append(
                f"{cancelled_plans} plan{'s' if cancelled_plans != 1 else ''}"
            )
        if cancelled_streams:
            parts.append(
                f"{cancelled_streams} stream{'s' if cancelled_streams != 1 else ''}"
            )
        if cancelled_interventions:
            parts.append(
                f"{cancelled_interventions} intervention"
                f"{'s' if cancelled_interventions != 1 else ''}"
            )
        if cancelled_tool_calls:
            parts.append(
                f"{cancelled_tool_calls} tool_call"
                f"{'s' if cancelled_tool_calls != 1 else ''}"
            )
        self._voice_status(
            f"✗ cancelled {' + '.join(parts)}", style=f"bold {_RED_MUTED}",
        )

    def action_toggle_panel(self) -> None:
        """ctrl+b — open or close the right panel.

        Smart targeting: when opening the panel, jump to the tab most relevant
        to the recent conv-pane focal point — events tab after an error,
        agents tab after skill activity. When closing, no tab change.

        Focus rescue: if focus was inside the panel when we hide it, the
        focused widget would otherwise become unreachable. Move focus back
        to the input bar before hiding so the user can keep typing.
        """
        self._panel_visible = not self._panel_visible
        panel = self.query_one("#right_panel", RightPanel)

        if self._panel_visible:
            panel.display = True
            if self._last_focal_tab:
                try:
                    panel.set_panel_type(self._last_focal_tab)
                except Exception:
                    pass
            # Wave-4 PC1: auto-focus the panel tabs so ``j`` / ``k`` /
            # ``space`` etc. immediately drive the panel's cursor +
            # preview, not the input bar. Previously the user had to
            # press Ctrl+B then Ctrl+O to start navigating — pressing
            # ``j`` after Ctrl+B alone inserted a literal "j" into
            # the input box. The close path's focus-rescue already
            # restores input focus when Ctrl+B closes the panel, so
            # the peek-while-typing flow stays intact (= Ctrl+B opens,
            # Ctrl+B closes, focus returns, text preserved).
            try:
                panel.focus_tabs()
            except Exception:
                pass
        else:
            # Closing: rescue focus if it lives inside the panel.
            focused = self.focused
            in_panel = focused is not None and (
                focused is panel
                or any(a is panel for a in focused.ancestors)
            )
            if in_panel:
                try:
                    self.query_one("#inputbar", InputBar).focus_input()
                except Exception:
                    pass
            panel.display = False

    def action_prev_turn(self) -> None:
        """ctrl+p — scroll the conversation log to the previous agent turn."""
        try:
            self.query_one("#conversation", ConversationView).jump_prev_turn()
        except Exception:
            pass

    def action_conv_scroll_page_up(self) -> None:
        """PageUp — scroll the conv log up one page without changing focus.

        Wave-4 AR5: complements the anchor-based Ctrl+P/N navigation
        with free-form keyboard scrolling. RichLog has ``can_focus=
        False`` so Textual's default Page Up doesn't reach it; we
        dispatch through the conv pane explicitly.
        """
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.scroll_page_up()
        except Exception:
            pass

    def action_conv_scroll_page_down(self) -> None:
        """PageDown — scroll the conv log down one page without changing focus."""
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.scroll_page_down()
        except Exception:
            pass

    def action_conv_scroll_line_up(self) -> None:
        """Alt+Up — scroll the conv log up one line without changing focus."""
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.scroll_line_up()
        except Exception:
            pass

    def action_conv_scroll_line_down(self) -> None:
        """Alt+Down — scroll the conv log down one line without changing focus."""
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.scroll_line_down()
        except Exception:
            pass

    def action_conv_scroll_home(self) -> None:
        """Alt+Home — jump the conv log to the top (oldest content)."""
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.scroll_to_top()
        except Exception:
            pass

    def action_conv_scroll_end(self) -> None:
        """Alt+End — jump to the conv log tail and re-arm auto-scroll."""
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.scroll_to_bottom()
        except Exception:
            pass

    def action_find_next(self) -> None:
        """Ctrl+G — cycle to the next ``/find`` match (wraps to first)."""
        router = self._outbox_router
        if router is None:
            return
        router.cycle_find(+1)

    def action_find_prev(self) -> None:
        """Ctrl+Shift+G — cycle to the previous ``/find`` match (wraps to last)."""
        router = self._outbox_router
        if router is None:
            return
        router.cycle_find(-1)

    def _maybe_emit_f3_tip(
        self,
        show_status_fn: "Callable[[str], None]",
    ) -> bool:
        """Emit the first-use F3 tip if it hasn't been shown this project.

        Reads ``tip_f3_seen`` from ``.reyn/tui_prefs.json``. When False
        (= first F3 press for this project), calls ``show_status_fn``
        with the onboarding message, flips the flag, and persists. On
        subsequent presses the tip is silently skipped.

        Returns True if the tip was emitted, False if already seen.

        Failure modes:
        - Prefs save failure → swallowed (= tip may re-fire next session,
          but that's better than crashing the action).
        - ``show_status_fn`` raising → swallowed (= conv pane mid-init).
        """
        from reyn.chat.tui.prefs import load_tui_prefs, save_tui_prefs

        root = self._project_root_path()
        prefs = load_tui_prefs(root)
        if prefs.get("tip_f3_seen", False):
            return False
        try:
            show_status_fn(
                "F3: drill-down expands phase history + tool calls for"
                " in-flight skills (mouse-click does the same)"
            )
        except Exception:
            pass
        prefs["tip_f3_seen"] = True
        try:
            save_tui_prefs(root, prefs)
        except Exception:
            pass
        return True

    def action_skill_expand_toggle(self) -> None:
        """F3 — toggle drill-down on every in-flight inline row.

        Keyboard companion to the mouse-click expand: instead of
        clicking each row individually, F3 flips them all at once.
        Targets both SkillActivityRow + ToolCallRow in-flight rows —
        a single keypress expands the full "what's happening right
        now" view (= skill phase trajectory + each running tool
        call's full args / result).

        Convergence: the new target state matches the FIRST in-flight
        row's current state flipped, then applied uniformly across
        all rows. Mixed-state sets (= one expanded, one collapsed)
        thus converge to a single state per keypress instead of
        oscillating per-row.

        No-op with a status hint when nothing is in flight in either
        widget kind (= the user pressed F3 expecting drill-down but
        nothing is currently running).

        First-use: shows a one-time onboarding tip via the conv-pane
        status bar (gated by ``tip_f3_seen`` in tui_prefs.json).
        """
        try:
            conv = self.query_one("#conversation", ConversationView)
        except Exception:
            return
        # First-use tip fires regardless of whether any rows are
        # in flight — teaching what F3 does is useful either way.
        tip_shown = self._maybe_emit_f3_tip(
            lambda msg: conv.show_status(msg, kind="general"),
        )
        skill_rows = conv.in_flight_skill_rows()
        tool_rows = conv.in_flight_tool_call_rows()
        rows: list = list(skill_rows) + list(tool_rows)
        if not rows:
            if tip_shown:
                # Tip replaces the "no rows" hint for this first press only;
                # auto-hide after ~4 s.
                self.set_timer(4.0, conv.hide_status)
            else:
                try:
                    conv.show_status(
                        "no active rows to expand", kind="general",
                    )
                    self.set_timer(2.0, conv.hide_status)
                except Exception:
                    pass
            return
        # Pick a single target state for THIS keypress so the set
        # converges. Without this, F3 on a mixed set (one expanded,
        # one collapsed) would flip each individually and the next
        # F3 press would oscillate — not what the user wants.
        target_expand = not rows[0].is_expanded
        for row in rows:
            if row.is_expanded != target_expand:
                row.toggle_expand()
        if tip_shown:
            # Auto-hide the tip after ~4 s once expand has been applied.
            self.set_timer(4.0, conv.hide_status)

    def action_focus_async_stack(self) -> None:
        """F4 — focus the bottom AsyncStackPanel for keyboard navigation.

        When the panel has entries, give it focus so j/k navigate
        through the rows. With no entries: surface a status hint
        rather than silently steal focus to an empty panel (which
        would trap the user — they'd press Esc to escape only to
        wonder what they accidentally focused).
        """
        try:
            from .widgets.async_stack_panel import AsyncStackPanel
            panel = self.query_one("#async-stack", AsyncStackPanel)
        except Exception:
            return
        if not panel.snapshot():
            try:
                conv = self.query_one("#conversation", ConversationView)
                conv.show_status(
                    "no active tasks in async strip", kind="general",
                )
                self.set_timer(2.0, conv.hide_status)
            except Exception:
                pass
            return
        panel.focus()

    def action_drill_failed_tool(self) -> None:
        """F7 — toggle expand on the most-recent failed ToolCallRow.

        Keyboard parity for the mouse-click expand path on tool-failure
        rows (W13 audit finding A#5). Three cases:

        1. A failed row is live (= still mounted as a widget, not yet
           flushed to the RichLog scroll history): toggle its expand state
           so the full failure reason becomes readable inline without
           switching tabs.

        2. A failed row exists but has already been flushed (= widget
           removed after ``_TOOL_CALL_MIN_DISPLAY_S``): surface a hint
           pointing the user at Ctrl+B -> Events tab where the full trace
           is always available.

        3. No failed row in this session: surface a "no recent tool
           failure" hint so the user knows the key registered (= not a
           silent no-op).
        """
        try:
            conv = self.query_one("#conversation", ConversationView)
        except Exception:
            return
        row = conv.latest_failed_tool_row()
        if row is None:
            try:
                conv.show_status("no recent tool failure", kind="general")
                self.set_timer(2.0, conv.hide_status)
            except Exception:
                pass
            return
        # Determine whether the row is still mounted (= live widget) or
        # already flushed into the RichLog (= widget removed).
        # Textual's ``is_mounted`` property reflects whether the widget
        # is currently in the DOM. A flushed row has been removed; its
        # Python object persists but the widget is no longer attached.
        try:
            still_mounted = row.is_mounted
        except Exception:
            still_mounted = False
        if not still_mounted:
            # Row already in scroll history -- point at events for full trace.
            try:
                conv.show_status(
                    "tool row flushed — Ctrl+B → Events tab for full trace",
                    kind="general",
                )
                self.set_timer(3.0, conv.hide_status)
            except Exception:
                pass
            return
        try:
            row.toggle_expand()
        except Exception:
            pass

    def action_toggle_timestamps(self) -> None:
        """F9 — toggle the HH:MM timestamp prefix in conv-pane message headers.

        With timestamps hidden, the body indent shrinks from col 8 to col 2,
        reclaiming horizontal space for content. The toggle applies to NEW
        messages only (= no re-render of past scroll history). State is
        persisted to ``tui_prefs.json`` so the choice survives a restart.
        """
        try:
            conv = self.query_one("#conversation", ConversationView)
        except Exception:
            return
        new_state = conv.toggle_timestamps()
        label = "on" if new_state else "off"
        try:
            conv.show_status(f"timestamps: {label}", kind="general")
            self.set_timer(2.0, conv.hide_status)
        except Exception:
            pass

    def action_next_turn(self) -> None:
        """ctrl+n — scroll the conversation log to the next agent turn."""
        try:
            self.query_one("#conversation", ConversationView).jump_next_turn()
        except Exception:
            pass

    def action_focus_toggle_panel(self) -> None:
        """ctrl+o — cycle focus: input → intervention buttons (if pending) → panel tabs → preview pane → input."""
        panel = self.query_one("#right_panel", RightPanel)
        focused = self.focused
        ancestors = [focused, *focused.ancestors] if focused else []
        in_panel = any(a is panel for a in ancestors)

        if not in_panel:
            # Check for a pending intervention widget — focus its first button
            # so the user can Tab through choices and press Space/Enter to pick one.
            try:
                conv = self.query_one("#conversation", ConversationView)
                iv_list = list(conv.query(InterventionWidget))
                if iv_list:
                    in_iv = any(a in iv_list for a in ancestors)
                    if not in_iv:
                        # Focus the first button (or the free-text Input if no buttons)
                        iv = iv_list[-1]  # most recent intervention
                        from textual.widgets import Button
                        from textual.widgets import Input as _Input
                        btns = list(iv.query(Button))
                        if btns:
                            btns[0].focus()
                            return
                        inputs = list(iv.query(_Input))
                        if inputs:
                            inputs[0].focus()
                            return
            except Exception:
                pass
            # Not in panel at all → move to panel tabs
            panel.focus_tabs()
            return

        # Determine whether focus is inside the preview pane specifically
        in_preview = False
        try:
            preview = panel.query_one("#preview-pane")
            in_preview = any(a is preview for a in ancestors)
        except Exception:
            pass

        if in_preview:
            # Preview pane → go back to input
            self.query_one("#inputbar", InputBar).focus_input()
        else:
            # Panel tabs → go to preview pane (if open) else input
            if panel.preview_visible:
                try:
                    panel.query_one("#preview-pane").focus()
                    return
                except Exception:
                    pass
            self.query_one("#inputbar", InputBar).focus_input()

    def action_panel_next_content(self) -> None:
        """ctrl+w — cycle to next panel tab (gated: panel visible only)."""
        self.query_one("#right_panel", RightPanel).cycle(+1)

    def action_panel_prev_content(self) -> None:
        """ctrl+shift+w — cycle to previous panel tab (gated: panel visible only)."""
        self.query_one("#right_panel", RightPanel).cycle(-1)

    def _panel_jump(self, panel_type: str) -> None:
        """Jump directly to ``panel_type`` tab, opening the panel if hidden.

        Single funnel for the seven Ctrl+1 .. Ctrl+7 quick-jump
        actions. The action_toggle_panel path may set the tab to the
        smart-Ctrl+B focal target (``_last_focal_tab``) on open;
        we override that with the user's explicit choice immediately
        afterwards, so Ctrl+N always lands on the requested tab.

        Defensive on lookup failure (= panel widget missing in tests
        that bypass standard compose).
        """
        try:
            panel = self.query_one("#right_panel", RightPanel)
        except Exception:
            return
        if not self._panel_visible:
            self.action_toggle_panel()
        try:
            panel.set_panel_type(panel_type)
        except Exception:
            pass

    def action_panel_jump_keys(self) -> None:
        """Ctrl+1 — open panel + jump to the Keys tab."""
        self._panel_jump("keys")

    def action_panel_jump_events(self) -> None:
        """Ctrl+2 — open panel + jump to the Events tab."""
        self._panel_jump("events")

    def action_panel_jump_agents(self) -> None:
        """Ctrl+3 — open panel + jump to the Agents tab."""
        self._panel_jump("agents")

    def action_panel_jump_memory(self) -> None:
        """Ctrl+4 — open panel + jump to the Memory tab."""
        self._panel_jump("memory")

    def action_panel_jump_cost(self) -> None:
        """Ctrl+5 — open panel + jump to the Cost tab."""
        self._panel_jump("cost")

    def action_panel_jump_docs(self) -> None:
        """Ctrl+6 — open panel + jump to the Docs tab."""
        self._panel_jump("docs")

    def action_panel_jump_pending(self) -> None:
        """Ctrl+7 — open panel + jump to the Pending tab."""
        self._panel_jump("pending")

    def action_event_filter_cycle(self) -> None:
        """f — rotate event filter (gated: events tab visible)."""
        self.query_one("#right_panel", RightPanel).cycle_event_filter()

    def action_event_tail_cycle(self) -> None:
        """t — rotate event tail count (gated: events tab visible)."""
        self.query_one("#right_panel", RightPanel).cycle_event_tail()

    # ── voice input (F2 / Esc) ─────────────────────────────────────────────

    def _voice_status(self, text: str, *, style: str = "dim " + _TEXT_BODY) -> None:
        """Write a short status line into the conversation pane."""
        try:
            from rich.text import Text as RichText
            t = RichText()
            t.append(text, style=style)
            self.query_one("#conversation", ConversationView)._write_log(t)
        except Exception:
            pass

    def _voice_set_header_state(self, state: str | None) -> None:
        """Drive the header voice badge from app-side voice transitions.

        Defensive on lookup failure (= header widget might not be
        mounted in tests that bypass standard compose). State values
        forwarded as-is — header itself validates the string.
        """
        try:
            self.query_one("#header", ReynHeader).set_voice_state(state)
        except Exception:
            pass

    async def action_voice_toggle(self) -> None:
        """F2 — toggle dictation (start ↔ stop+transcribe→inject into InputBar).

        Errors are surfaced as conv-pane status lines; never crash the TUI.
        """
        # Lazy import + lazy instantiation to keep base install dep-free.
        if self._voice_input is None:
            try:
                from .voice import VoiceInput
            except Exception as exc:
                self._voice_status(
                    f"✗ voice input unavailable ({exc}); install with: "
                    "pip install \"reyn[voice]\"",
                    style="bold red",
                )
                return
            cfg_voice = self._voice_config()
            if cfg_voice is not None and not cfg_voice.enabled:
                self._voice_status(
                    "✗ voice input disabled in config (set voice.enabled: true)",
                    style="bold red",
                )
                return
            if not VoiceInput.available():
                self._voice_status(
                    "✗ voice extras not installed; run: "
                    "pip install \"reyn[voice]\"  (and brew install portaudio)",
                    style="bold red",
                )
                return
            kwargs = {}
            if cfg_voice is not None:
                kwargs = {
                    "model": cfg_voice.model,
                    "language": cfg_voice.language,
                    "device": cfg_voice.device,
                    "compute_type": cfg_voice.compute_type,
                    "sample_rate": cfg_voice.sample_rate,
                    "cpu_threads": cfg_voice.cpu_threads,
                    "num_workers": cfg_voice.num_workers,
                    "max_duration_s": cfg_voice.max_duration_s,
                }
            self._voice_input = VoiceInput(**kwargs)
            # Watchdog: every second, check whether the active recording
            # has exceeded its configured `max_duration_s`. If so, cancel
            # so we don't accumulate gigabytes of audio when the user
            # walks away mid-dictation. Cheap (one comparison) and self-
            # disarming when no recording is active.
            self.set_interval(1.0, self._voice_watchdog_tick)

        if self._voice_busy:
            return  # already transcribing — ignore re-presses

        if not self._voice_input.is_recording:
            # Status BEFORE the (now-async) start_recording so the user
            # sees feedback even when CoreAudio takes a moment to hand
            # over the device.
            self._voice_status(
                "🔴 starting mic… (Ctrl+R stop · Enter stop+send · Esc cancel)"
            )
            await self._yield_for_render()
            try:
                await self._voice_input.start_recording()
            except Exception as exc:
                self._voice_status(f"✗ voice recording failed: {exc}", style="bold red")
                self._voice_set_header_state(None)
                return
            # Replace the "starting…" line with the live recording
            # message once the stream is actually open.
            self._voice_status(
                "🔴 recording — Ctrl+R stop · Enter stop+send · Esc cancel"
            )
            self._voice_set_header_state("recording")
            # Take the InputBox out of the focus rotation + key-input path
            # while the mic is live: prevents accidental letters typed
            # during dictation from landing in the input field, and Tab /
            # Shift+Tab now cycle only among right-panel widgets. Restored
            # on every code path that ends recording.
            self._voice_set_input_locked(True)
            # Start the model load in the background while the user is
            # speaking. First-time load is ~30 s (download + CTranslate2
            # init). Without this pre-warm the user hits a long opaque
            # block on the second Ctrl+R press; with it the second press
            # usually returns in ~1 s.
            asyncio.create_task(self._voice_input.preload_model())
        else:
            self._voice_busy = True
            self._voice_set_header_state("transcribing")
            # Choose a status message that sets honest expectations: if
            # the model isn't hot yet the wait will be long, so say so.
            if self._voice_input.model_loaded:
                self._voice_status("⏳ transcribing…")
            else:
                self._voice_status(
                    "⏳ transcribing… (loading model — first run only, "
                    "~30 s on slow connection)",
                )
            await self._yield_for_render()
            try:
                text, diag = await self._voice_input.stop_recording()
            except Exception as exc:
                self._voice_status(f"✗ transcription failed: {exc}", style="bold red")
                self._voice_busy = False
                self._voice_set_input_locked(False)
                self._voice_set_header_state(None)
                return
            self._voice_busy = False
            self._voice_set_input_locked(False)
            self._voice_set_header_state(None)
            if not text:
                self._voice_show_empty_diagnostic(diag)
                return
            try:
                self.query_one("#inputbar", InputBar).append_text(text)
            except Exception:
                pass
            preview = text if len(text) <= 60 else text[:57] + "…"
            dur = diag.get("duration_s", 0.0)
            self._voice_status(
                f"✓ inserted ({dur:.1f}s): {preview}", style="dim " + _TEXT_BODY
            )

    async def action_voice_stop_and_submit(self) -> None:
        """Enter while recording — stop + transcribe + submit immediately.

        Skips the manual edit step. Useful when the user is confident the
        dictation is right and wants to send without touching the keyboard
        again. Gated by ``check_action`` so plain Enter in the input bar
        still submits normally; this binding only fires while the mic is
        live.
        """
        from .voice import _vlog as _voice_vlog
        _voice_vlog("action_voice_stop_and_submit: entered")
        if self._voice_input is None or not self._voice_input.is_recording:
            _voice_vlog("action_voice_stop_and_submit: no active recording, bail")
            return
        if self._voice_busy:
            _voice_vlog("action_voice_stop_and_submit: already busy, bail")
            return
        self._voice_busy = True
        self._voice_set_header_state("transcribing")
        if self._voice_input.model_loaded:
            self._voice_status("⏳ transcribing & sending…")
        else:
            self._voice_status(
                "⏳ transcribing & sending… (loading model — first run only)"
            )
        _voice_vlog("action_voice_stop_and_submit: yielding for render")
        await self._yield_for_render()
        _voice_vlog("action_voice_stop_and_submit: calling stop_recording")
        try:
            text, diag = await self._voice_input.stop_recording()
        except Exception as exc:
            _voice_vlog(f"action_voice_stop_and_submit: stop_recording raised {exc!r}")
            self._voice_status(f"✗ transcription failed: {exc}", style="bold red")
            self._voice_busy = False
            self._voice_set_input_locked(False)
            self._voice_set_header_state(None)
            return
        _voice_vlog(
            f"action_voice_stop_and_submit: stop_recording returned "
            f"len={len(text)} reason={diag.get('reason')}"
        )
        self._voice_busy = False
        self._voice_set_input_locked(False)
        self._voice_set_header_state(None)
        if not text:
            self._voice_show_empty_diagnostic(diag)
            return
        # Status FIRST — so even if the submit path takes a moment, the
        # user sees the transcript landed and is being sent.
        preview = text if len(text) <= 60 else text[:57] + "…"
        dur = diag.get("duration_s", 0.0)
        self._voice_status(
            f"✓ sent ({dur:.1f}s): {preview}", style="dim " + _TEXT_BODY
        )
        await self._yield_for_render()
        _voice_vlog("action_voice_stop_and_submit: about to append + submit")
        # Bypass `inputbar.action_submit_or_confirm()` — that path threads
        # through the slash-picker + private _submit and has been observed
        # to leave focus + the busy-status line in a bad state when called
        # programmatically. Instead, do the same three things _submit does
        # (record history, clear textarea, post UserSubmitted), then
        # explicitly refocus the input bar.
        try:
            inputbar = self.query_one("#inputbar", InputBar)
            inputbar.append_text(text)
            ta = inputbar._textarea()
            full_text = ta.text.strip() if ta is not None else text
            if ta is not None:
                ta.clear()
            inputbar.focus_input()
            self.post_message(InputBar.UserSubmitted(full_text))
        except Exception as exc:
            _voice_vlog(f"action_voice_stop_and_submit: submit raised {exc!r}")
            self._voice_status(
                f"✗ auto-submit failed: {exc}", style="bold red",
            )
            return
        _voice_vlog("action_voice_stop_and_submit: completed")

    def _voice_set_input_locked(self, locked: bool) -> None:
        """Lock or unlock the InputBox during voice recording.

        ``locked=True``  → TextArea ``disabled = True`` (Textual blocks
                           keyboard input AND moves focus to the next
                           focusable widget). If a right panel is
                           visible, focus is steered to its tabs so the
                           user can still navigate the panel during
                           dictation.
        ``locked=False`` → restore TextArea + push focus back to the
                           input bar so the user can edit / send the
                           transcript.
        """
        try:
            inputbar = self.query_one("#inputbar", InputBar)
        except Exception:
            return
        ta = inputbar._textarea()
        if ta is None:
            return
        if locked:
            ta.disabled = True
            # If the right panel is open, give the user a sensible
            # initial focus so Tab cycling works inside it. If not,
            # Textual's auto-focus-move on `disabled=True` will land
            # focus on App (= no widget), which is fine — the priority
            # App-level voice bindings (Ctrl+R / Enter / Esc) still fire.
            if self._panel_visible:
                try:
                    self.query_one("#right_panel", RightPanel).focus_tabs()
                except Exception:
                    pass
        else:
            ta.disabled = False
            inputbar.focus_input()

    async def _yield_for_render(self) -> None:
        """Make absolutely sure a just-written status line reaches the screen
        before we begin a long synchronous operation.

        Single ``await asyncio.sleep(0)`` is not enough — it yields once,
        but Textual's compositor schedules across multiple ticks
        (widget refresh → layout → render → flush). When we then enter
        ``asyncio.to_thread(...)`` the worker thread can hold the GIL
        long enough to starve those follow-up ticks, leaving the previous
        frame on screen until the worker returns — visually identical to
        a TUI freeze.

        Belt + braces: explicit refresh on App + the conversation pane
        (which is where the status line lives), then ``sleep(0.1)`` to
        let the compositor actually run. 100 ms is imperceptible to the
        user but guaranteed-enough for the status line to materialise.
        """
        try:
            self.refresh()
        except Exception:
            pass
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.refresh(layout=True)
        except Exception:
            pass
        await asyncio.sleep(0.1)

    def _voice_show_empty_diagnostic(self, diag: dict) -> None:
        """Build an actionable status line for an empty transcription.

        Shared by both the edit-then-send path (Ctrl+R twice) and the
        immediate-send path (Ctrl+R + Enter). Tells the user whether
        the mic captured audio, whether it was silent, or whether the
        audio was fine but Whisper recognised nothing.
        """
        dur = diag.get("duration_s", 0.0)
        peak = diag.get("peak", 0.0)
        reason = diag.get("reason", "silent")
        if reason == "no_audio" or dur < 0.3:
            self._voice_status(
                "(no audio captured — mic permission? wrong device?)",
                style=f"dim {_RED_MUTED}",
            )
        elif reason == "silent" or peak < 0.01:
            self._voice_status(
                f"(silent capture: {dur:.1f}s, peak={peak:.3f}) — "
                "check mic gain / system input device",
                style=f"dim {_RED_MUTED}",
            )
        else:
            self._voice_status(
                f"(no speech recognised in {dur:.1f}s, peak={peak:.3f}) — "
                "try speaking closer / louder, or set a larger model",
                style=f"dim {_RED_MUTED}",
            )

    def _voice_watchdog_tick(self) -> None:
        """Per-second tick: auto-stop runaway recordings, preserving audio.

        Triggered by ``set_interval(1.0, ...)`` after the first VoiceInput
        is created. Self-disarming — does nothing when no recording is
        active. Caps memory growth (16 kHz mono float32 ≈ 64 KB/s, so an
        8-hour idle recording would otherwise pile up ~1.8 GB of buffer).

        Behaviour at the cap: instead of dropping the buffer, we run the
        same stop+transcribe+append path as a manual second-Ctrl+R press.
        The transcript lands in the InputBox so nothing the user already
        said is lost, and a follow-up Ctrl+R session will concatenate
        further dictation into the same input field via
        ``InputBar.append_text``.
        """
        if self._voice_input is None or not self._voice_input.is_recording:
            return
        if self._voice_busy:
            return  # already transcribing — let it finish
        cap = self._voice_input.max_duration_s
        if cap <= 0:
            return  # 0 = uncapped, opt-out
        elapsed = self._voice_input.recording_elapsed_s
        if elapsed < cap:
            return
        # Cap reached — kick off transcribe-and-insert as a fire-and-forget
        # task. Setting _voice_busy here (= synchronously) prevents a
        # subsequent watchdog tick from racing into a second start.
        self._voice_busy = True
        self._voice_status(
            f"⏰ recording cap reached ({cap:.0f}s) — transcribing & inserting…",
            style=f"dim {_RED_MUTED}",
        )
        asyncio.create_task(self._voice_auto_stop_and_insert())

    async def _voice_auto_stop_and_insert(self) -> None:
        """Watchdog continuation — transcribe the captured buffer + append
        to the InputBox + leave the input bar focused so Ctrl+R can extend.

        Mirrors the second-Ctrl+R path in ``action_voice_toggle`` but
        without re-checking ``_voice_busy`` (the caller already set it).
        """
        if self._voice_input is None:
            self._voice_busy = False
            self._voice_set_input_locked(False)
            return
        await self._yield_for_render()
        try:
            text, diag = await self._voice_input.stop_recording()
        except Exception as exc:
            self._voice_status(
                f"✗ auto-stop transcription failed: {exc}", style="bold red",
            )
            self._voice_busy = False
            self._voice_set_input_locked(False)
            return
        self._voice_busy = False
        self._voice_set_input_locked(False)
        if not text:
            self._voice_show_empty_diagnostic(diag)
            return
        try:
            inputbar = self.query_one("#inputbar", InputBar)
            inputbar.append_text(text)
            inputbar.focus_input()
        except Exception:
            pass
        preview = text if len(text) <= 60 else text[:57] + "…"
        dur = diag.get("duration_s", 0.0)
        self._voice_status(
            f"✓ inserted ({dur:.1f}s, auto-stopped): {preview}  — "
            "Ctrl+R to continue dictating, Enter to send",
            style="dim " + _TEXT_BODY,
        )

    def action_voice_cancel(self) -> None:
        """Esc — cancel voice recording, dismiss slash picker, dismiss rewind
        menu, or close panel.

        Priority:
          1. If recording → cancel recording.
          2. **Else if InputBar is in slash-entry → dismiss picker + clear**
             prefix (= wave-2 P2).
          3. Else if the rewind menu is open → dismiss it (ADR-0038 1f).
          4. Else if side panel is visible → close it.

        Gated by check_action so this never fires when no condition holds
        (allowing InputBar's own Esc binding to handle slash-picker
        dismissal in the no-other-overlay case).
        """
        if self._voice_input is not None and self._voice_input.is_recording:
            self._voice_input.cancel()
            self._voice_set_input_locked(False)
            self._voice_set_header_state(None)
            self._voice_status("✗ recording cancelled", style="dim " + _TEXT_DIM)
            self._reset_clean_esc()
            return
        try:
            input_bar = self.query_one("#inputbar", InputBar)
            if input_bar.dismiss_slash_prefix():
                self._reset_clean_esc()
                return
        except Exception:
            pass
        # ADR-0038 1f: dismiss the rewind menu before the side panel — the
        # menu is the most-recently-opened overlay, so Esc should close it
        # first (matches "abort the visible thing" priority).
        if self._rewind_menu is not None:
            self._dismiss_rewind_menu()
            self._reset_clean_esc()
            return
        if self._panel_visible:
            self.action_toggle_panel()
            self._reset_clean_esc()
            return
        # #1546: nothing was dismissed → this is a *truly clean* Esc. A second
        # clean Esc within the window opens the /rewind picker (Esc-Esc).
        now = _now_monotonic()
        if self._last_clean_esc_ts and (now - self._last_clean_esc_ts) < _ESC_ESC_WINDOW_S:
            self._reset_clean_esc()
            self._open_rewind_menu()
        else:
            self._last_clean_esc_ts = now
            self._show_esc_esc_hint()

    _ESC_HINT_TEXT = "⏪ Esc again to rewind"

    def _reset_clean_esc(self) -> None:
        """Disarm the pending Esc-Esc first-tap (#1546).

        Pure ts reset — touches no status/timer, so the four dismiss branches
        (and the check_action slash-entry branch) can call it without clobbering
        their own status. The hint sticky is self-clearing via its own timer
        (``_clear_esc_esc_hint``), which only hides when *its* text is showing.
        """
        self._last_clean_esc_ts = 0.0

    def _cancel_esc_hint_timer(self) -> None:
        timer = self._esc_hint_timer
        self._esc_hint_timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

    def _show_esc_esc_hint(self) -> None:
        """Sticky "Esc again to rewind" cue on the first clean Esc, auto-cleared
        after the window so it doesn't linger once the double-tap chance lapses.
        Re-arming cancels any prior timer; opening the picker is handled by
        ``mount_rewind_menu``'s own status."""
        self._cancel_esc_hint_timer()
        try:
            conv = self.query_one("#conversation", ConversationView)
            conv.show_status(self._ESC_HINT_TEXT, kind="general")
            self._esc_hint_timer = self.set_timer(
                _ESC_ESC_WINDOW_S, self._clear_esc_esc_hint,
            )
        except Exception:
            pass

    def _clear_esc_esc_hint(self) -> None:
        # Window lapsed: the double-tap chance is gone, so the first-tap is no
        # longer pending (keeps esc_esc_pending semantically accurate).
        self._esc_hint_timer = None
        self._last_clean_esc_ts = 0.0
        # Hide the sticky ONLY if our hint is still the one showing — never
        # clobber a status another path set in the meantime (e.g. a dismiss
        # breadcrumb, or the open picker's own cue).
        try:
            from .widgets.sticky_status import StickyStatus
            conv = self.query_one("#conversation", ConversationView)
            sticky = conv.query_one("#sticky-status", StickyStatus)
            if self._ESC_HINT_TEXT in sticky.snapshot().get("body", ""):
                conv.hide_status()
        except Exception:
            pass

    # ── rewind menu (ADR-0038 1f) ───────────────────────────────────────────

    def _dismiss_rewind_menu(self) -> None:
        """Unmount the rewind picker, if mounted. Decoupled from the
        intervention unmount path — a plain ``widget.remove()`` + state clear."""
        menu = self._rewind_menu
        self._rewind_menu = None
        if menu is not None:
            try:
                menu.remove()
            except Exception:
                pass
            # Clear the scrolled-up "⏪ rewind menu below ↓" cue (#1550) so it
            # doesn't linger after the picker is gone. Mirrors the
            # intervention_resolved hide_status on both answer paths.
            try:
                self.query_one("#conversation", ConversationView).hide_status()
            except Exception:
                pass

    def _open_rewind_menu(self) -> None:
        """Open the inline /rewind fork picker (ADR-0038 2b, always-tree).

        Builds the branch tree from ``list_branches()`` + the lineage-tagged
        ``list_rewind_points(include_abandoned=True)`` and mounts it. When there
        are no checkpoints (or no registry), writes a system message instead of
        mounting an empty picker.
        """
        try:
            conv = self.query_one("#conversation", ConversationView)
        except Exception:
            return
        rows = self._build_rewind_tree_rows()
        if not rows:
            conv.render_message(
                OutboxMessage(kind="system", text="⏪ no checkpoints to rewind to yet")
            )
            return
        # Replace any already-open menu (idempotent re-open).
        self._dismiss_rewind_menu()
        self._rewind_menu = conv.mount_rewind_menu(tree_rows=rows)

    def _build_rewind_tree_rows(self) -> list[dict]:
        """Branch-tree rows for the fork picker, or [] when unavailable.

        Converts the ``Branch`` dataclasses to dicts and groups the
        lineage-tagged checkpoints by ``branch_id`` (the substrate already did
        the lineage-correct membership — 2a). Best-effort: any registry error
        yields [] so the picker degrades to the "no checkpoints" notice rather
        than crashing the TUI.
        """
        import dataclasses

        from .widgets._branch_tree import build_branch_tree_rows
        registry = self._agent_registry
        if registry is None:
            return []
        try:
            branches = [dataclasses.asdict(b) for b in registry.list_branches()]
            checkpoints = registry.list_rewind_points(include_abandoned=True)
        except Exception:
            return []
        return build_branch_tree_rows(branches, checkpoints)

    def action_rewind_prev(self) -> None:
        """↑ while the rewind menu is open — highlight the previous (older) checkpoint."""
        if self._rewind_menu is not None:
            self._rewind_menu.move_selection(-1)

    def action_rewind_next(self) -> None:
        """↓ while the rewind menu is open — highlight the next (newer) checkpoint."""
        if self._rewind_menu is not None:
            self._rewind_menu.move_selection(+1)

    async def action_rewind_confirm(self) -> None:
        """Enter while the picker is open — checkout the highlighted checkpoint.

        Unified checkout (ADR-0038 D8): the same op whether the node is on the
        live branch (= undo) or a dead branch (= fork-switch) — the user just
        "goes to this checkpoint".
        """
        menu = self._rewind_menu
        if menu is None:
            return
        point = menu.selected_point()
        self._dismiss_rewind_menu()
        if point is not None:
            await self._do_checkout(int(point["seq"]))

    async def _do_checkout(self, target_seq: int) -> None:
        """Invoke ``AgentRegistry.checkout(seq)`` and write a timeline breadcrumb.

        ``checkout`` is the unified time-travel primitive (active-seq = undo,
        dead-branch seq = fork-switch); the guard-lifted reset-record moves both
        substrates to the target's lineage.
        """
        try:
            conv = self.query_one("#conversation", ConversationView)
        except Exception:
            conv = None
        registry = self._agent_registry
        if registry is None:
            if conv is not None:
                conv.render_message(
                    OutboxMessage(kind="error", text="⏪ checkout unavailable (no registry)")
                )
            return
        try:
            result = await registry.checkout(target_seq)
        except Exception as exc:  # noqa: BLE001 — surface the reason in-timeline
            if conv is not None:
                conv.render_message(OutboxMessage(kind="error", text=f"⏪ checkout failed: {exc}"))
            return
        if conv is not None:
            agents = result.get("agents", [])
            conv.render_message(OutboxMessage(
                kind="system",
                text=(
                    f"⏪ checked out to seq {result.get('target_n', target_seq)} "
                    f"· {len(agents)} agent(s) reset · in-flight cancelled"
                ),
            ))

    def _voice_config(self):
        """Best-effort fetch of the user's voice config block."""
        try:
            from reyn.config import load_config
            return load_config().voice
        except Exception:
            return None

    def check_action(self, action: str, parameters):
        """Gate panel-scoped bindings to when the panel is visible/relevant."""
        if action == "focus_toggle_panel":
            # Allowed when the panel is visible OR when an intervention
            # is mounted — the action's first branch focuses the chip
            # buttons, and gating it on panel_visible alone left users
            # with no keyboard path to the chips when the panel was
            # closed (forcing Ctrl+B first, then Ctrl+O).
            if self._panel_visible:
                return True
            try:
                conv = self.query_one("#conversation", ConversationView)
                return bool(list(conv.query(InterventionWidget)))
            except Exception:
                return False
        if action in {"panel_next_content", "panel_prev_content"}:
            return self._panel_visible
        if action in {"event_filter_cycle", "event_tail_cycle"}:
            if not self._panel_visible:
                return False
            try:
                return self.query_one("#right_panel", RightPanel).panel_type == "events"
            except Exception:
                return False
        if action in {"rewind_prev", "rewind_next", "rewind_confirm"}:
            # ADR-0038 1f: navigation keys are live only while the /rewind
            # picker is mounted. Otherwise they fall through to the InputBar
            # (↑/↓ history, Enter submit).
            return self._rewind_menu is not None
        if action == "voice_cancel":
            # Intercept Esc when there's an overlay/recording to dismiss:
            # voice recording, the rewind menu, or the side panel.
            if self._voice_input is not None and self._voice_input.is_recording:
                return True
            if self._rewind_menu is not None:
                return True
            if self._panel_visible:
                return True
            # #1546: also grab a *truly clean* Esc — nothing dismissable here
            # AND no InputBar slash-entry to clear — so action_voice_cancel can
            # run the Esc-Esc double-tap detection. When InputBar HAS a
            # slash-entry we return False so its own Esc binding clears the
            # prefix (not stolen). has_slash_entry() is the public read.
            try:
                if self.query_one("#inputbar", InputBar).has_slash_entry():
                    # The slash-clearing Esc is consumed by InputBar — the App
                    # never runs action_voice_cancel for it, so this branch is
                    # the ONLY place to reset the pending Esc-Esc first-tap.
                    # Without it, `Esc(arm) → /x → Esc(clear) → Esc` false-fires
                    # the double-tap (tui-coder #1554 repro). Same reset
                    # discipline as the four dismiss branches.
                    self._reset_clean_esc()
                    return False
                return True
            except Exception:
                return False
        if action == "voice_stop_and_submit":
            # Same gate as voice_cancel: Enter only behaves as "stop+send"
            # while recording. Outside of recording it falls through to
            # the InputBar's own Enter binding (= normal submit).
            return self._voice_input is not None and self._voice_input.is_recording
        return True

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_session(self) -> "ChatSession | None":
        if self._agent_registry is None:
            return None
        return self._agent_registry.attached_session()

    def _project_root_path(self) -> Path | None:
        """Return the attached registry's project root, or None.

        Used by ``prefs.load_tui_prefs`` / ``prefs.save_tui_prefs`` to
        locate ``<project_root>/.reyn/tui_prefs.json``. Defensive: the
        registry may be missing the ``_project_root`` attribute on
        certain test / remote-mode bootstrap paths, in which case the
        prefs file falls back to "no persistence" — toggle state still
        works in memory.
        """
        if self._agent_registry is None:
            return None
        return getattr(self._agent_registry, "_project_root", None)


async def run_tui(
    registry: "AgentRegistry",
    *,
    agent_name: str = "default",
    model: str = "",
    budget_tracker=None,
    banner: bool = False,
    no_restore: bool = False,
) -> None:
    """Entry point called from cli/commands/chat.py when TUI mode is selected."""
    app = ReynTUIApp(
        registry=registry,
        agent_name=agent_name,
        model=model,
        budget_tracker=budget_tracker,
        banner=banner,
        no_restore=no_restore,
    )
    await app.run_async()
