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


# Re-export for backward compatibility — moved to a shared module so the
# right_panel widgets can use it without creating an import cycle through
# app_outbox.
from ._clipboard import copy_to_clipboard as _copy_to_clipboard  # noqa: F401


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
            "intervention_resolved":    self._on_intervention_resolved,
            "status":                   self._on_status,
            "trace":                    self._on_trace,
            # NOTE: "skill_done" outbox kind was removed in FP-0011.
            # Skill completion is now signalled via "skill done: <status>"
            # trace text from ChatEventForwarder (workflow_finished /
            # workflow_aborted events) and handled in app._handle_trace_for_skill_row.
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
            try:
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
            except Exception as exc:
                # A handler crash used to silently break the outbox loop —
                # the TUI froze on its last frame with no events flowing
                # and no indication of the cause. Surface the failure as
                # a one-line conv-pane error so the user can see WHY
                # things stopped, then keep draining (= one bad message
                # doesn't kill all subsequent ones).
                import traceback
                tb = traceback.format_exception_only(type(exc), exc)[-1].strip()
                from rich.text import Text as _RichText
                err = _RichText()
                err.append("✗ ", style="bold red")
                err.append(f"outbox handler [{msg.kind}] raised: ", style="red")
                err.append(tb, style="red")
                try:
                    conv._write_log(err)
                except Exception:
                    pass

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
        """`__copy_last_reply__` — /copy slash; pipe a buffered reply to OS clipboard.

        ``msg.text`` carries the parsed slash argument:
          - empty           → copy the latest reply (= ``reply_at(1)``)
          - ``"list"``      → show how many replies are buffered + dim hint
          - integer N >= 1  → copy ``reply_at(N)`` (N = 1 is newest)
          - anything else   → surface a usage hint and stop

        Workaround for the TUI's mouse-capture preventing native click-and-
        drag selection. Tries platform-native binaries in order; if none
        succeed, surfaces a status line so the user knows the workaround
        wasn't available.
        """
        arg = (msg.text or "").strip()

        if arg.lower() == "list":
            count = conv.recent_reply_count()
            if count == 0:
                conv.show_status("no replies buffered yet", kind="general")
            else:
                conv.show_status(
                    f"{count} reply{'s' if count != 1 else ''} buffered "
                    f"— /copy 1..{count}",
                    kind="general",
                )
            self._app.set_timer(2.5, conv.hide_status)
            return

        # Parse N. Empty → latest (n=1); digits → that index. Anything else
        # is a typo — surface the usage hint rather than silently copying
        # the latest, since that would mask the user's mistake.
        if arg == "":
            n = 1
        elif arg.isdigit():
            n = int(arg)
            if n <= 0:
                conv.show_status(
                    "/copy index must be ≥ 1 (1 = newest)", kind="error",
                )
                self._app.set_timer(2.5, conv.hide_status)
                return
        else:
            conv.show_status(
                "usage: /copy [N] or /copy list", kind="error",
            )
            self._app.set_timer(2.5, conv.hide_status)
            return

        text = conv.reply_at(n)
        if text is None:
            buffered = conv.recent_reply_count()
            if buffered == 0:
                conv.show_status("nothing to copy yet", kind="general")
            else:
                conv.show_status(
                    f"reply {n} not in buffer "
                    f"(only {buffered} available — /copy list)",
                    kind="error",
                )
            self._app.set_timer(2.5, conv.hide_status)
            return

        ok, label = _copy_to_clipboard(text)
        if ok:
            n_chars = len(text)
            tag = "latest" if n == 1 else f"#{n}"
            conv.show_status(
                f"copied reply {tag} ({n_chars} chars) via {label}",
                kind="general",
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

        Computes ``queued_extra`` from the session's intervention registry
        so the widget can render a persistent ``+N more pending`` badge.
        The sticky ``awaiting answer (N queued)`` status gets overwritten
        by the next ``thinking…`` event, so we need an inline persistent
        signal instead.
        """
        conv.hide_status()
        iv_id = msg.meta.get("intervention_id", "")
        raw_choices = msg.meta.get("choices")
        choices = None
        if raw_choices:
            # Forward the full choice shape (label / id / hotkey / default)
            # so the widget can render `[h] Label` prefixes and highlight a
            # default option. Missing keys fall back to safe blanks — callers
            # that historically only set label+id keep working.
            choices = [
                {
                    "label": c["label"],
                    "id": c["id"],
                    "hotkey": c.get("hotkey") or "",
                    "default": bool(c.get("default", False)),
                }
                for c in raw_choices
            ]
        # Best-effort queue depth — the registry owns the canonical count;
        # any failure (no session attached, attribute missing on a stubbed
        # session in tests) falls back to 0 so we never break the mount.
        queued_extra = 0
        try:
            session = self._app._get_session()
            registry = getattr(session, "_interventions", None) if session else None
            if registry is not None:
                queued_extra = max(0, registry.queued_count() - 1)
        except Exception:
            queued_extra = 0
        self._app._mount_intervention(
            conv, msg.text, iv_id, choices, queued_extra=queued_extra,
        )

    def _on_intervention_resolved(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`intervention_resolved` — remove the inline widget when the user
        answered via text input (not a chip button click).

        The chip-button path calls ``InterventionWidget._submit`` which already
        calls ``self.remove()``.  The text-input path (Enter in the InputBar)
        routes through session._deliver_answer_to without touching the widget,
        so we need this outbox message to clean up the orphaned widget.
        """
        iv_id = msg.meta.get("iv_id", "")
        widget_id = f"iv_{iv_id[:8]}"
        try:
            widget = self._app.query_one(f"#{widget_id}")
            widget.remove()
        except Exception:
            pass

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
