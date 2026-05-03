"""ReynTUIApp — Textual TUI for `reyn chat`.

Layout:
  ┌─ ReynHeader ─────────────────────────────────────────────────────┐  dock=top h=1
  │                                                                   │
  │  ConversationView (RichLog + inline widgets)  1fr  │  RightPanel │  dock=right (hidden by default)
  │                                                                   │
  ├───────────────────────────────────────────────────────────────────┤
  │  InputBar (Input + hint label)                        dock=bottom │
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
from textual.widgets import Label

from .widgets import ReynHeader, ConversationView, InputBar, RightPanel
from .widgets.input_bar import InputBar as _InputBar  # same, but alias for clarity

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession


# Command palette item for Tab autocomplete
from textual.widgets import OptionList
from textual.widgets.option_list import Option


class CommandPaletteOverlay(OptionList):
    """Inline command palette that appears above the Input on Tab press.

    Mounted (hidden) inside InputBar in compose(). Toggling the "visible"
    class flips display between none and block. InputBar is `height: auto`
    so the bar grows upward when the palette becomes visible.
    """

    DEFAULT_CSS = """
    CommandPaletteOverlay {
        dock: top;
        height: auto;
        max-height: 12;
        min-width: 40;
        background: #1a1a1a;
        border: tall #C8553D;
        display: none;
    }
    CommandPaletteOverlay.visible {
        display: block;
    }
    """


class ReynTUIApp(App):
    """Main Textual application for `reyn chat`."""

    CSS_PATH = Path(__file__).parent / "theme.tcss"

    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+d", "quit_tui", "Quit", priority=True),
        Binding("ctrl+l", "clear_conversation", "Clear", priority=True),
        Binding("ctrl+c", "cancel_inflight", "Cancel", priority=True),
        Binding("ctrl+b", "toggle_panel", "Panel", priority=True, show=False),
        Binding("tab", "palette_next", "Commands / next", priority=True, show=False),
        Binding("n", "palette_next_only", "Next", priority=True, show=False),
        Binding("p", "palette_prev", "Prev", priority=True, show=False),
        Binding("j", "palette_next_only", "Next", priority=True, show=False),
        Binding("k", "palette_prev", "Prev", priority=True, show=False),
        Binding("ctrl+o", "focus_toggle_panel", "Focus panel", priority=True, show=False),
        Binding("ctrl+w", "panel_next_content", "Next tab", priority=True, show=False),
        Binding("ctrl+shift+w", "panel_prev_content", "Prev tab", priority=False, show=False),
        Binding("f", "event_filter_cycle", "Filter events", priority=True, show=False),
        Binding("t", "event_tail_cycle", "Tail events", priority=True, show=False),
        Binding("backspace", "palette_backspace", "Back", priority=True, show=False),
        Binding("escape", "close_palette", "Close palette", priority=True, show=False),
    ]

    def __init__(
        self,
        *,
        registry: "AgentRegistry | None" = None,
        agent_name: str = "default",
        model: str = "",
        budget_tracker=None,
    ) -> None:
        super().__init__()
        self._agent_registry = registry   # NOTE: NOT _registry (Textual internal)
        self._agent_name = agent_name
        self._model = model
        self._budget_tracker = budget_tracker
        self._outbox_task: asyncio.Task | None = None
        self._palette_visible = False
        self._panel_visible = False
        self._all_slash_names: list[str] = []
        self._cancel_event: asyncio.Event = asyncio.Event()

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
        # Load slash names from registry
        from reyn.chat.slash import REGISTRY as SLASH_REGISTRY
        self._all_slash_names = SLASH_REGISTRY.names()

        inputbar = self.query_one("#inputbar", InputBar)
        inputbar.update_slash_names(self._all_slash_names)

        # Pre-mount the command palette (hidden) inside InputBar so that
        # Tab handling only needs to populate + toggle the visible class —
        # no race with the async mount lifecycle.
        palette = CommandPaletteOverlay(id="palette")
        inputbar.mount(palette)

        inputbar.focus_input()

        # Show startup banner
        conv = self.query_one("#conversation", ConversationView)
        from rich.text import Text
        t = Text()
        t.append("Reyn ", style="bold #C8553D")
        t.append("— type a message or ")
        t.append("Tab", style="bold")
        t.append(" for commands, ")
        t.append("Ctrl+D", style="bold")
        t.append(" to quit")
        conv._write_log(t)

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

    # ── message handlers from widgets ─────────────────────────────────────────

    async def on_input_bar_user_submitted(self, msg: InputBar.UserSubmitted) -> None:
        """User hit Enter — dispatch to session or slash registry."""
        text = msg.text.strip()
        if not text:
            return

        # Show user message in conversation
        conv = self.query_one("#conversation", ConversationView)
        from rich.text import Text
        from reyn.chat.outbox import OutboxMessage
        user_t = Text()
        user_t.append("you    ", style="bold")
        user_t.append(text)
        conv._write_log(user_t)

        # Close palette if open
        self._close_palette()

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

    def on_input_bar_open_palette(self, msg: InputBar.OpenPalette) -> None:
        """Tab — open/update slash command palette."""
        self._open_palette(prefix=msg.prefix)

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
        """ctrl+b — close palette if open, then toggle the right panel."""
        if self._palette_visible:
            self._close_palette()
        self._panel_visible = not self._panel_visible
        self.query_one("#right_panel", RightPanel).display = self._panel_visible

    def action_focus_toggle_panel(self) -> None:
        """ctrl+o — toggle focus between input and panel tabs (gated: panel visible)."""
        panel = self.query_one("#right_panel", RightPanel)
        focused = self.focused
        in_panel = focused is not None and any(
            a is panel for a in [focused, *focused.ancestors]
        )
        if in_panel:
            self.query_one("#inputbar", InputBar).focus_input()
        else:
            panel.focus_tabs()

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

    def action_close_palette(self) -> None:
        self._close_palette()
        inputbar = self.query_one("#inputbar", InputBar)
        inputbar.focus_input()

    def action_open_palette(self) -> None:
        """Tab — open the slash command palette filtered by current input."""
        try:
            inp = self.query_one("#inputbar", InputBar).query_one("#input")
            prefix = inp.value
        except Exception:
            prefix = ""
        self._open_palette(prefix=prefix)

    def action_palette_next(self) -> None:
        """Tab / Ctrl+N — open the palette, or advance selection if already open."""
        if self._palette_visible:
            self._move_palette_cursor(1)
        else:
            self.action_open_palette()

    def action_palette_next_only(self) -> None:
        """n / j — advance selection; only fires when palette is visible
        (gated via check_action so plain letters don't capture in input)."""
        self._move_palette_cursor(1)

    def action_palette_prev(self) -> None:
        """Shift+Tab / Ctrl+P / p / k — move selection up; gated via
        check_action so plain letters don't capture when input has focus."""
        if self._palette_visible:
            self._move_palette_cursor(-1)

    def _move_palette_cursor(self, delta: int) -> None:
        """Advance / retreat the palette selection by `delta` (±1)."""
        try:
            overlay = self.query_one("#palette", CommandPaletteOverlay)
        except Exception:
            return
        count = overlay.option_count
        if count == 0:
            return
        current = overlay.highlighted if overlay.highlighted is not None else -1
        overlay.highlighted = (current + delta) % count

    def check_action(self, action: str, parameters):
        """Disable palette-only bindings when the palette is closed.

        Backspace, Shift+Tab, Ctrl+P, Esc should fall through to the focused
        Input widget when no palette is visible. Without this, the priority
        bindings would swallow them — Backspace wouldn't delete characters,
        Esc wouldn't reach prompt-toolkit-style chord handlers, etc.
        """
        if action in {
            "palette_backspace",
            "palette_prev",
            "palette_next_only",
            "close_palette",
        }:
            return self._palette_visible
        if action in {"focus_toggle_panel", "panel_next_content", "panel_prev_content"}:
            return self._panel_visible
        if action == "palette_next" and self._panel_visible:
            focused = self.focused
            try:
                panel = self.query_one("#right_panel", RightPanel)
                if focused is not None and any(
                    a is panel for a in [focused, *focused.ancestors]
                ):
                    return False
            except Exception:
                pass
        if action in {"event_filter_cycle", "event_tail_cycle"}:
            if not self._panel_visible:
                return False
            try:
                return self.query_one("#right_panel", RightPanel).panel_type == "events"
            except Exception:
                return False
        return True

    def action_palette_backspace(self) -> None:
        """Backspace while palette is open: close + delete one char from input."""
        self._close_palette()
        try:
            inputbar = self.query_one("#inputbar", InputBar)
            inp = inputbar.query_one("#input")
            if inp.value:
                # Drop the last character; cursor follows.
                inp.value = inp.value[:-1]
                inp.cursor_position = len(inp.value)
            inputbar.focus_input()
        except Exception:
            pass

    # ── command palette ───────────────────────────────────────────────────────

    def _open_palette(self, prefix: str = "") -> None:
        """Show or refresh the command palette."""
        from reyn.chat.slash import REGISTRY

        # Filter commands by prefix (after /), sorted alphabetically.
        # Hidden commands (e.g. easter eggs) only appear when typed by name.
        cmd_prefix = prefix[1:] if prefix.startswith("/") else ""
        all_cmds = sorted(
            (c for c in REGISTRY.all_commands() if not c.hidden),
            key=lambda c: c.name,
        )
        matches = [c for c in all_cmds if c.name.startswith(cmd_prefix)]

        if not matches:
            self._close_palette()
            return

        try:
            overlay = self.query_one("#palette", CommandPaletteOverlay)
        except Exception:
            # Should not happen — palette is pre-mounted in on_mount.
            return

        overlay.clear_options()
        for cmd in matches:
            overlay.add_option(Option(f"/{cmd.name}  — {cmd.summary}", id=cmd.name))

        overlay.add_class("visible")
        try:
            self.query_one("#inputbar", InputBar).add_class("palette-open")
        except Exception:
            pass
        # Highlight the first match so Enter is immediately useful — no
        # need to press Ctrl+N before selecting the only candidate.
        overlay.highlighted = 0
        overlay.focus()
        self._palette_visible = True

    def _close_palette(self) -> None:
        try:
            overlay = self.query_one("#palette", CommandPaletteOverlay)
            overlay.remove_class("visible")
        except Exception:
            pass
        try:
            self.query_one("#inputbar", InputBar).remove_class("palette-open")
        except Exception:
            pass
        self._palette_visible = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """User selected a command from the palette."""
        if event.option.id:
            cmd_name = event.option.id
            inputbar = self.query_one("#inputbar", InputBar)
            # Set input to the slash command
            try:
                inp = inputbar.query_one("#input")
                inp.value = f"/{cmd_name} "
                inp.cursor_position = len(inp.value)
                inp.focus()
            except Exception:
                pass
        self._close_palette()

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
) -> None:
    """Entry point called from cli/commands/chat.py when TUI mode is selected."""
    app = ReynTUIApp(
        registry=registry,
        agent_name=agent_name,
        model=model,
        budget_tracker=budget_tracker,
    )
    await app.run_async()
