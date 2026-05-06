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
        Binding("f", "event_filter_cycle", "Filter events", priority=True, show=False),
        Binding("t", "event_tail_cycle", "Tail events", priority=True, show=False),
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
        """Drain registry.repl_outbox and render each message."""
        if self._agent_registry is None:
            return
        conv = self.query_one("#conversation", ConversationView)
        header = self.query_one("#header", ReynHeader)
        current_stream_id: str | None = None

        while True:
            try:
                msg = await self._agent_registry.repl_outbox.get()
            except asyncio.CancelledError:
                break

            if msg.kind == "__end__":
                break

            if msg.kind == "__attach_request__":
                # Handled by AgentRegistry._forwarder; we just update our state
                new_name = msg.text
                if new_name and self._agent_registry is not None:
                    self._agent_name = new_name
                    self.query_one("#header", ReynHeader).refresh_status(
                        agent_name=new_name,
                    )
                continue

            if msg.kind == "__matrix__":
                # Easter egg: /matrix slash command lands here.
                from reyn.chat.tui.widgets.matrix import MatrixScreen
                self.push_screen(MatrixScreen())
                continue

            if msg.kind == "__donut__":
                # Easter egg: /donut slash command lands here.
                from reyn.chat.tui.widgets.donut import DonutScreen
                self.push_screen(DonutScreen())
                continue

            if msg.kind == "__stream_start__":
                # Begin a streaming row
                current_stream_id = msg.meta.get("msg_id", id(msg))
                agent = self._agent_name
                conv.begin_stream(current_stream_id, agent)
                continue

            if msg.kind == "__stream_chunk__":
                # Append to the current streaming row
                if current_stream_id is not None:
                    conv.append_stream(current_stream_id, msg.text)
                continue

            if msg.kind == "__stream_end__":
                if current_stream_id is not None:
                    conv.end_stream(current_stream_id)
                    current_stream_id = None
                # Update status line after stream ends
                self._maybe_refresh_status(header)
                continue

            # Intervention: mount inline widget for structured response
            if msg.kind == "intervention":
                iv_id = msg.meta.get("intervention_id", "")
                raw_choices = msg.meta.get("choices")
                choices = None
                if raw_choices:
                    choices = [(c["label"], c["id"]) for c in raw_choices]
                self._mount_intervention(conv, msg.text, iv_id, choices)
                continue

            # Regular message
            conv.render_message(msg)

            # Refresh status after agent/skill_done messages
            if msg.kind in {"agent", "skill_done"}:
                self._maybe_refresh_status(header)

            # Track live skill execution state for Agents tab
            if msg.kind == "trace" and msg.meta.get("skill_name"):
                self._update_skill_exec(msg)
                self._push_exec_state()
            elif msg.kind == "skill_done":
                run_id = msg.meta.get("run_id", "")
                if run_id:
                    self._skill_exec.pop(run_id, None)
                    self._push_exec_state()

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
            }
            self._skill_exec[run_id] = existing
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

    # ── message handlers from widgets ─────────────────────────────────────────

    async def on_input_bar_user_submitted(self, msg: InputBar.UserSubmitted) -> None:
        """User hit Enter — dispatch to session or slash registry."""
        text = msg.text.strip()
        if not text:
            return

        # Show user message in conversation
        conv = self.query_one("#conversation", ConversationView)
        from rich.text import Text

        from reyn.chat.tui.widgets.conversation import _msg_header
        conv._write_log(_msg_header("you", "bold #4abbb5", "#1f5856"))
        conv._write_log(Text(text))
        conv._write_log(Text(""))

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
        """Cancel the in-flight skill/model call on the attached session."""
        session = self._get_session()
        if session is None:
            return
        # Cancel all running skills
        for task in list(getattr(session, "running_skills", {}).values()):
            if not task.done():
                task.cancel()

    def action_toggle_panel(self) -> None:
        """ctrl+b — open or close the right panel."""
        self._panel_visible = not self._panel_visible
        self.query_one("#right_panel", RightPanel).display = self._panel_visible

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
