"""Outbox routing for ReynTUIApp.

Drains ``registry.repl_outbox`` and dispatches each :class:`OutboxMessage`
to the right widget (conv pane, sticky status, error box, skill activity row,
preview pane, screen header). Lives in its own module to keep ``app.py``
focused on composition / lifecycle.

Design:
  - One ``OutboxRouter`` instance per app, constructed with a back-reference
    to the App. State that needs to live across messages (active streaming
    id, per-turn cost snapshot, smart-Ctrl+B focal tab, …) is carried on the
    App, not the router — the router is a thin dispatcher.
  - Each ``msg.kind`` has a dedicated ``_on_<kind>`` method. The dispatch
    table (`HANDLERS`) maps kinds to those methods. Adding a new sentinel
    kind is one line in the table + one method.
  - Handlers return ``None`` for "continue", or the sentinel string ``"stop"``
    when the outbox loop should exit (only ``__end__`` does today).
  - The fallback path (no handler matched) calls ``conv.render_message(msg)``
    and post-processes ``kind="agent"`` for status / cost suffix.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

from reyn.chat.outbox import OutboxMessage

from .widgets import ConversationView, ReynHeader, RightPanel

if TYPE_CHECKING:
    from .app import ReynTUIApp


# Sentinel returned by handlers that want to break the outbox loop.
_STOP = "stop"


# Order of clipboard tools we try for /copy. First match that succeeds wins.
# Each entry is (binary_name, argv_tail, label).
_CLIPBOARD_TOOLS: tuple[tuple[str, list[str], str], ...] = (
    ("pbcopy",   [],            "pbcopy"),         # macOS
    ("wl-copy",  [],            "wl-copy"),        # Wayland
    ("xclip",    ["-selection", "clipboard"], "xclip"),  # X11
    ("xsel",     ["--clipboard", "--input"], "xsel"),    # X11 fallback
    ("clip",     [],            "clip"),           # Windows
)


def _copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Pipe ``text`` to a platform clipboard tool. Returns ``(ok, tool_label)``.

    Looked up via ``shutil.which`` so the user only needs one of them on
    PATH. We avoid hard-coding the OS because users may run, e.g., xclip
    inside a Linux VM regardless of the host platform.
    """
    import shutil
    import subprocess

    for binary, tail, label in _CLIPBOARD_TOOLS:
        path = shutil.which(binary)
        if path is None:
            continue
        try:
            subprocess.run(
                [path, *tail],
                input=text.encode("utf-8"),
                check=True,
                timeout=2.0,
            )
            return True, label
        except Exception:
            continue
    return False, ""


class OutboxRouter:
    """Drain + dispatch loop for the registry's outbox queue."""

    def __init__(self, app: "ReynTUIApp") -> None:
        self._app = app
        # Dispatch table — each entry maps a `msg.kind` to its handler.
        # Methods on `self` are bound, so we can reference them directly.
        self.HANDLERS: dict[str, Callable[..., str | None]] = {
            "__end__":                  self._on_end,
            "__attach_request__":       self._on_attach_request,
            "__matrix__":               self._on_matrix,
            "__donut__":                self._on_donut,
            "__cost_inline_toggle__":   self._on_cost_inline_toggle,
            "__expand_last_reply__":    self._on_expand_last_reply,
            "__copy_last_reply__":      self._on_copy_last_reply,
            "__docs_filter__":          self._on_docs_filter,
            "__stream_start__":         self._on_stream_start,
            "__stream_chunk__":         self._on_stream_chunk,
            "__stream_end__":           self._on_stream_end,
            "intervention":             self._on_intervention,
            "status":                   self._on_status,
            "trace":                    self._on_trace,
            "skill_done":               self._on_skill_done,
            "error":                    self._on_error,
        }

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Drain the registry's repl_outbox until it ends or is cancelled."""
        app = self._app
        if app._agent_registry is None:
            return
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)

        while True:
            try:
                msg = await app._agent_registry.repl_outbox.get()
            except asyncio.CancelledError:
                break

            handler = self.HANDLERS.get(msg.kind)
            if handler is not None:
                result = handler(msg, conv, header)
                if result == _STOP:
                    break
                continue

            # Default: render the message + post-process agent turns.
            conv.render_message(msg)
            if msg.kind == "agent":
                app._maybe_refresh_status(header)
                app._maybe_render_cost_suffix(conv)

    # ── per-kind handlers (sentinel kinds first) ──────────────────────────────

    def _on_end(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> str:
        """`__end__` — registry signals shutdown; loop should break.

        Also clears any leftover ``⟳ thinking…`` sticky so the final
        TUI frame on shutdown isn't a phantom indicator.
        """
        conv.hide_status()
        return _STOP

    def _on_attach_request(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__attach_request__` — agent switched; refresh header label."""
        app = self._app
        new_name = msg.text
        if new_name and app._agent_registry is not None:
            app._agent_name = new_name
            header.refresh_status(agent_name=new_name)

    def _on_matrix(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__matrix__` — easter egg; push the matrix screen."""
        from reyn.chat.tui.widgets.matrix import MatrixScreen
        self._app.push_screen(MatrixScreen())

    def _on_donut(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__donut__` — easter egg; push the donut screen."""
        from reyn.chat.tui.widgets.donut import DonutScreen
        self._app.push_screen(DonutScreen())

    def _on_cost_inline_toggle(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__cost_inline_toggle__` — /cost-inline slash command sets this.

        Body text is "on" / "off" / empty (toggle). Shows a 2.5 s sticky
        status indicating the new state.
        """
        app = self._app
        want = (msg.text or "").strip().lower()
        if want == "on":
            app._cost_inline_enabled = True
        elif want == "off":
            app._cost_inline_enabled = False
        else:
            app._cost_inline_enabled = not app._cost_inline_enabled
        state = "on" if app._cost_inline_enabled else "off"
        conv.show_status(f"cost-inline {state}", kind="general")
        app.set_timer(2.5, conv.hide_status)

    def _on_expand_last_reply(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__expand_last_reply__` — /expand slash; flush truncated reply."""
        if not conv.expand_last_reply():
            conv.show_status("nothing to expand", kind="general")
            self._app.set_timer(2.0, conv.hide_status)

    def _on_copy_last_reply(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__copy_last_reply__` — /copy slash; pipe last reply to OS clipboard.

        Workaround for the TUI's mouse-capture preventing native click-and-
        drag selection. Tries platform-native binaries in order; if none
        succeed, surfaces a status line so the user knows the workaround
        wasn't available.
        """
        text = conv.last_reply_text()
        if not text:
            conv.show_status("nothing to copy yet", kind="general")
            self._app.set_timer(2.0, conv.hide_status)
            return
        ok, label = _copy_to_clipboard(text)
        if ok:
            n_chars = len(text)
            conv.show_status(
                f"copied {n_chars} chars via {label}", kind="general",
            )
        else:
            conv.show_status(
                "no clipboard tool found "
                "(install pbcopy / xclip / wl-copy / xsel)",
                kind="error",
            )
        self._app.set_timer(2.5, conv.hide_status)

    def _on_docs_filter(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__docs_filter__` — /docs-filter; set or clear the docs tab filter."""
        app = self._app
        substr = (msg.text or "").strip()
        try:
            panel = app.query_one("#right_panel", RightPanel)
            panel.set_docs_filter(substr)
            panel.set_panel_type("docs")
        except Exception:
            pass
        msg_text = f"docs filter: {substr}" if substr else "docs filter cleared"
        conv.show_status(msg_text, kind="general")
        app.set_timer(2.0, conv.hide_status)

    def _on_stream_start(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__stream_start__` — begin a streaming agent reply row."""
        app = self._app
        app._current_stream_id = msg.meta.get("msg_id", id(msg))
        # Hide the "thinking…" sticky now that the reply is starting
        conv.hide_status()
        conv.begin_stream(app._current_stream_id, app._agent_name)

    def _on_stream_chunk(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__stream_chunk__` — append text to the active streaming row."""
        app = self._app
        if app._current_stream_id is not None:
            conv.append_stream(app._current_stream_id, msg.text)

    def _on_stream_end(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__stream_end__` — seal the streaming row + refresh status."""
        app = self._app
        if app._current_stream_id is not None:
            conv.end_stream(app._current_stream_id)
            app._current_stream_id = None
        app._maybe_refresh_status(header)
        # A4: render per-turn cost suffix when opt-in is enabled
        app._maybe_render_cost_suffix(conv)

    def _on_intervention(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`intervention` — mount inline ask_user widget.

        Hides the sticky ``⟳ thinking…`` indicator first: while the
        agent is waiting for a human answer, "thinking" is misleading
        — the system is blocked on user input, not on the model.
        """
        conv.hide_status()
        iv_id = msg.meta.get("intervention_id", "")
        raw_choices = msg.meta.get("choices")
        choices = None
        if raw_choices:
            choices = [(c["label"], c["id"]) for c in raw_choices]
        self._app._mount_intervention(conv, msg.text, iv_id, choices)

    def _on_status(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`status` — route to sticky bar (not the log)."""
        conv.show_status(msg.text, kind="thinking")

    def _on_trace(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`trace` — drive a SkillActivityRow when a skill_name is attached."""
        if not msg.meta.get("skill_name"):
            return
        app = self._app
        app._handle_trace_for_skill_row(conv, msg)
        app._update_skill_exec(msg)
        app._push_exec_state()

    def _on_skill_done(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`skill_done` — finish the skill activity row + remember focal tab."""
        app = self._app
        run_id = msg.meta.get("run_id", "")
        if run_id:
            conv.finish_skill_row(
                run_id,
                success=True,
                reason=msg.meta.get("summary", "") or "",
            )
            app._skill_exec.pop(run_id, None)
            app._push_exec_state()
            app._last_focal_tab = "agents"
        app._maybe_refresh_status(header)

    def _on_error(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`error` — render via conv (mounts ErrorBox) + remember focal tab.

        Also clears the sticky ``⟳ thinking…`` indicator: a turn that
        ends in error never reaches `__stream_start__` / `agent`, so
        without this the indicator would stick around indefinitely.
        """
        conv.hide_status()
        conv.render_message(msg)
        self._app._last_focal_tab = "events"


__all__ = ["OutboxRouter"]
