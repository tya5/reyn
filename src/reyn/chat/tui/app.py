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

from .widgets import ConversationView, InputBar, ReynHeader, RightPanel

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession


class ReynTUIApp(App):
    """Main Textual application for `reyn chat`."""

    CSS_PATH = Path(__file__).parent / "theme.tcss"

    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+d", "quit_tui", "Quit", priority=True),
        Binding("ctrl+l", "clear_conversation", "Clear", priority=True),
        Binding("ctrl+c", "cancel_inflight", "Cancel", priority=True),
        Binding("ctrl+b", "toggle_panel", "Panel", priority=True, show=False),
        Binding("ctrl+o", "focus_toggle_panel", "Focus panel", priority=True, show=False),
        Binding("ctrl+p", "prev_turn", "Prev turn", priority=True, show=False),
        Binding("ctrl+n", "next_turn", "Next turn", priority=True, show=False),
        Binding("f", "event_filter_cycle", "Filter events", priority=True, show=False),
        Binding("t", "event_tail_cycle", "Tail events", priority=True, show=False),
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
        Binding("escape", "voice_cancel", "Voice cancel", priority=True, show=False),
        Binding("ctrl+backslash", "screenshot", "Screenshot", priority=True, show=False),
    ]

    _REYN_THEME = Theme(
        name="reyn",
        primary="#C8553D",
        accent="#C8553D",
        dark=True,
        variables={
            "block-cursor-background": "#C8553D",
            "block-cursor-foreground": "#ffffff",
        },
    )

    def __init__(
        self,
        *,
        registry: "AgentRegistry | None" = None,
        agent_name: str = "default",
        model: str = "",
        budget_tracker=None,
        banner: bool = False,
    ) -> None:
        super().__init__()
        self.register_theme(self._REYN_THEME)
        self.theme = "reyn"
        self._agent_registry = registry   # NOTE: NOT _registry (Textual internal)
        self._agent_name = agent_name
        self._model = model
        self._budget_tracker = budget_tracker
        self._banner = banner
        self._outbox_task: asyncio.Task | None = None
        self._panel_visible = False
        self._cancel_event: asyncio.Event = asyncio.Event()
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
                    rt.append(f"{key}  ", style="dim #555555")
                    rt.append(val, style="#dddddd")
                conv._write_log(rt)
            conv._write_log(Text("  Gives you the reins.", style="dim #555555"))
            conv._write_log(Text("─" * 38, style="#2a2a2a"))

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
        await OutboxRouter(self).run()

    def _mount_intervention(
        self,
        conv: ConversationView,
        text: str,
        iv_id: str,
        choices: list[tuple[str, str]] | None = None,
    ) -> None:
        """Mount an InterventionWidget inline in the conversation view.

        When `choices` is provided (from meta["choices"]), chip buttons are
        rendered. The user's answer (chip label or free text) is routed via
        session._maybe_answer_oldest_intervention — which matches hotkeys /
        choice labels against the pending intervention.
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
        )

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
            header.refresh_status(
                agent_name=self._agent_name,
                model=self._model,
                tokens_today=snap.get("daily_tokens", 0),
                tokens_cap=snap.get("daily_tokens_cap"),
                cost_usd=snap.get("daily_cost_usd", 0.0),
                cost_cap=snap.get("daily_cost_usd_cap"),
            )
        except Exception:
            pass

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
        # Text pattern: "phase started: <phase_name>"
        if text.startswith("phase started: "):
            phase = text[len("phase started: "):].strip()
            existing["phase"] = phase
            existing["phase_visits"] = existing.get("phase_visits", 0) + 1

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
          - "<phase> → <next> ..."         → ignored (phase-completed details
            are visible in the right panel events tab)
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
            conv.start_skill_row(run_id, skill_name)
            # Track phase visit count from existing exec state
            existing = self._skill_exec.get(run_id) or {}
            visit = int(existing.get("phase_visits", 0)) + 1
            conv.update_skill_phase(run_id, phase, visit=visit)
            self._last_focal_tab = "agents"
        elif text.startswith("skill done: "):
            # FP-0011: skill_narrator removed → skill_done outbox kind gone.
            # ChatEventForwarder now forwards workflow_finished / workflow_aborted
            # as "skill done: <status>" so the row can stop spinning here.
            status = text[len("skill done: "):].strip()
            conv.finish_skill_row(run_id, success=(status == "finished"), reason="")
            self._skill_exec.pop(run_id, None)
            self._push_exec_state()
            self._last_focal_tab = "agents"

    def _maybe_render_cost_suffix(self, conv: ConversationView) -> None:
        """A4 — emit a dim per-turn cost suffix line when /cost-inline is on."""
        if not self._cost_inline_enabled:
            return
        if self._budget_tracker is None:
            return
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
            conv.render_cost_suffix(delta_tokens, delta_cost, elapsed)
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
        # Remember this message so the next skill run that fires can
        # tag itself with "triggered_by". Cleared / overwritten on each
        # subsequent submit, so a long-tail skill that finishes after
        # the user has typed again still keeps the message that
        # actually started it (= we capture eagerly on first trace).
        self._latest_user_message = text

        # Get the attached session
        session = self._get_session()

        if text.startswith("/"):
            # Handle /quit and /exit locally
            cmd_body = text[1:].split()[0] if len(text) > 1 else ""
            if cmd_body in {"quit", "exit"}:
                await self.action_quit_tui()
                return
            # Dispatch to session._maybe_handle_slash (which uses registry)
            if session is not None:
                await session._maybe_handle_slash(text)
            else:
                # No session — still try the registry
                await self._dispatch_slash_no_session(text, conv)
        else:
            if session is not None:
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

        Visibility: clears the sticky ``⟳ thinking…`` indicator AND writes
        a one-line summary to the conv pane reporting how many skills /
        plans were actually cancelled. Without this summary the user
        couldn't tell whether Ctrl+C did anything (= "did it really
        cancel, or was nothing in flight?").
        """
        try:
            conv = self.query_one("#conversation", ConversationView)
        except Exception:
            conv = None
        if conv is not None:
            conv.hide_status()

        session = self._get_session()
        if session is None:
            self._voice_status(
                "(nothing to cancel — no session attached)",
                style="dim #aa6666",
            )
            return

        cancelled_skills = 0
        for task in list(getattr(session, "running_skills", {}).values()):
            if not task.done():
                task.cancel()
                cancelled_skills += 1
        cancelled_plans = 0
        for task in list(getattr(session, "running_plans", {}).values()):
            if not task.done():
                task.cancel()
                cancelled_plans += 1

        if cancelled_skills == 0 and cancelled_plans == 0:
            self._voice_status(
                "(nothing in-flight to cancel)", style="dim #555555",
            )
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
        self._voice_status(
            f"✗ cancelled {' + '.join(parts)}", style="bold #aa6666",
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

    def action_next_turn(self) -> None:
        """ctrl+n — scroll the conversation log to the next agent turn."""
        try:
            self.query_one("#conversation", ConversationView).jump_next_turn()
        except Exception:
            pass

    def action_focus_toggle_panel(self) -> None:
        """ctrl+o — cycle focus: input → panel tabs → preview pane → input."""
        panel = self.query_one("#right_panel", RightPanel)
        focused = self.focused
        ancestors = [focused, *focused.ancestors] if focused else []
        in_panel = any(a is panel for a in ancestors)

        if not in_panel:
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

    def action_event_filter_cycle(self) -> None:
        """f — rotate event filter (gated: events tab visible)."""
        self.query_one("#right_panel", RightPanel).cycle_event_filter()

    def action_event_tail_cycle(self) -> None:
        """t — rotate event tail count (gated: events tab visible)."""
        self.query_one("#right_panel", RightPanel).cycle_event_tail()

    # ── voice input (F2 / Esc) ─────────────────────────────────────────────

    def _voice_status(self, text: str, *, style: str = "dim #aaaaaa") -> None:
        """Write a short status line into the conversation pane."""
        try:
            from rich.text import Text as RichText
            t = RichText()
            t.append(text, style=style)
            self.query_one("#conversation", ConversationView)._write_log(t)
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
                return
            # Replace the "starting…" line with the live recording
            # message once the stream is actually open.
            self._voice_status(
                "🔴 recording — Ctrl+R stop · Enter stop+send · Esc cancel"
            )
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
                return
            self._voice_busy = False
            self._voice_set_input_locked(False)
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
                f"✓ inserted ({dur:.1f}s): {preview}", style="dim #aaaaaa"
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
            return
        _voice_vlog(
            f"action_voice_stop_and_submit: stop_recording returned "
            f"len={len(text)} reason={diag.get('reason')}"
        )
        self._voice_busy = False
        self._voice_set_input_locked(False)
        if not text:
            self._voice_show_empty_diagnostic(diag)
            return
        # Status FIRST — so even if the submit path takes a moment, the
        # user sees the transcript landed and is being sent.
        preview = text if len(text) <= 60 else text[:57] + "…"
        dur = diag.get("duration_s", 0.0)
        self._voice_status(
            f"✓ sent ({dur:.1f}s): {preview}", style="dim #aaaaaa"
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
                style="dim #aa6666",
            )
        elif reason == "silent" or peak < 0.01:
            self._voice_status(
                f"(silent capture: {dur:.1f}s, peak={peak:.3f}) — "
                "check mic gain / system input device",
                style="dim #aa6666",
            )
        else:
            self._voice_status(
                f"(no speech recognised in {dur:.1f}s, peak={peak:.3f}) — "
                "try speaking closer / louder, or set a larger model",
                style="dim #aa6666",
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
            style="dim #aa6666",
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
            style="dim #aaaaaa",
        )

    def action_voice_cancel(self) -> None:
        """Esc — discard the current recording without transcribing.

        Gated so it doesn't shadow the InputBar's own Esc handler when
        nothing is being recorded.
        """
        if self._voice_input is None or not self._voice_input.is_recording:
            return
        self._voice_input.cancel()
        self._voice_set_input_locked(False)
        self._voice_status("✗ recording cancelled", style="dim #555555")

    def _voice_config(self):
        """Best-effort fetch of the user's voice config block."""
        try:
            from reyn.config import load_config
            return load_config().voice
        except Exception:
            return None

    def check_action(self, action: str, parameters):
        """Gate panel-scoped bindings to when the panel is visible/relevant."""
        if action in {"focus_toggle_panel", "panel_next_content", "panel_prev_content"}:
            return self._panel_visible
        if action in {"event_filter_cycle", "event_tail_cycle"}:
            if not self._panel_visible:
                return False
            try:
                return self.query_one("#right_panel", RightPanel).panel_type == "events"
            except Exception:
                return False
        if action == "voice_cancel":
            # Only intercept Esc while we're actually recording — otherwise
            # the InputBar / SlashPicker / preview pane should keep using it.
            return self._voice_input is not None and self._voice_input.is_recording
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


async def run_tui(
    registry: "AgentRegistry",
    *,
    agent_name: str = "default",
    model: str = "",
    budget_tracker=None,
    banner: bool = False,
) -> None:
    """Entry point called from cli/commands/chat.py when TUI mode is selected."""
    app = ReynTUIApp(
        registry=registry,
        agent_name=agent_name,
        model=model,
        budget_tracker=budget_tracker,
        banner=banner,
    )
    await app.run_async()
