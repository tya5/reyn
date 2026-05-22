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


# Body prefixes emitted by ``planner.py`` for plan-progress status updates.
# Used by ``_on_status`` to recognise a plan-sourced sticky body that
# should not be overwritten by an unrelated router-loop status update.
# Kept here (not in planner.py) because the priority policy is a TUI
# UX choice, not a planner contract.
_PLAN_SOURCED_BODY_PREFIXES: tuple[str, ...] = (
    "plan step ",
    "plan started ",
    "リトライ ",
)


def _is_plan_sourced_body(body: str) -> bool:
    """Return True when ``body`` looks like a plan-sourced status emission."""
    stripped = body.lstrip()
    return any(stripped.startswith(p) for p in _PLAN_SOURCED_BODY_PREFIXES)


# Bulky / free-form keys we want to summarise rather than inline in the
# ToolCallRow's args / result line. Same set the forwarder/op_runtime
# helpers use — keeps the visual contract consistent across surfaces.
_TOOL_ARG_BULKY_FIELDS = frozenset({
    "content", "new_string", "old_string", "body", "preview",
})

# Keys to skip when formatting tool result for line-2 display: they're
# already encoded in the tool name on line 1 (= ``file__read(...)``
# already carries ``kind=file`` + ``op=read``). Surfacing them again
# in the result snippet is noise that pushes more-informative fields
# (= ``status`` / ``exit_code`` / specific data) off the truncated line.
_TOOL_RESULT_REDUNDANT_KEYS = frozenset({"kind", "op"})


def _format_tool_args(args: dict | None) -> str:
    """Compact ``key=value, ...`` repr of dispatcher args for ToolCallRow.

    Returns "" when ``args`` is empty / non-dict — ToolCallRow renders
    the tool name with empty parens in that case. Bulky string fields
    collapse to ``<N chars>`` so the line stays short.
    """
    if not isinstance(args, dict) or not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        if key in _TOOL_ARG_BULKY_FIELDS and isinstance(value, str) and len(value) > 24:
            parts.append(f"{key}=<{len(value)} chars>")
            continue
        s = str(value)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{key}={s}")
    return ", ".join(parts)


# Output budget for the assembled result snippet. ToolCallRow re-truncates
# at terminal width for very narrow terminals, but the formatter itself
# caps total length to keep ``<N chars>`` placeholder fields atomic — if
# the cell-aware truncation downstream had to cut into a placeholder
# (= ``content=<3 cha…``), the placeholder would lose its meaning.
# Drop trailing fields whole instead of truncating mid-placeholder.
_TOOL_RESULT_BUDGET_CHARS = 80


def _format_tool_result(result) -> str:
    """Compact one-line repr of dispatcher result for the ToolCallRow row-2.

    Accepts dict / str / None; degrades to "" when result has nothing
    presentable. Caps total length at ``_TOOL_RESULT_BUDGET_CHARS`` by
    dropping trailing fields whole rather than truncating mid-string, so
    ``<N chars>`` placeholders never get cut into incomprehensible
    fragments (= ``content=<3 cha…`` instead of ``content=<3 chars>``
    or the field dropped entirely).
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result[:117] + "..." if len(result) > 120 else result
    if not isinstance(result, dict):
        return str(result)[:120]
    parts: list[str] = []
    for key, value in result.items():
        if key in _TOOL_RESULT_REDUNDANT_KEYS:
            continue
        if key in _TOOL_ARG_BULKY_FIELDS and isinstance(value, str) and len(value) > 24:
            parts.append(f"{key}=<{len(value)} chars>")
            continue
        s = str(value)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{key}={s}")
    # Drop trailing fields whole until the joined string fits the budget.
    # Preserves atomic shape of ``<N chars>`` placeholders + ``key=value``
    # pairs — partial fields would be noise. Keeps ``status`` / earlier
    # context fields first because dict iteration is insertion-order, and
    # op handlers tend to emit ``status`` / primary signals first.
    while parts and len(", ".join(parts)) > _TOOL_RESULT_BUDGET_CHARS:
        parts.pop()
    return ", ".join(parts)


# Re-export for backward compatibility — moved to a shared module so the
# right_panel widgets can use it without creating an import cycle through
# app_outbox.
from ._clipboard import copy_to_clipboard as _copy_to_clipboard  # noqa: F401


class OutboxRouter:
    """Drain + dispatch loop for the registry's outbox queue."""

    def __init__(self, app: "ReynTUIApp") -> None:
        self._app = app
        # Tracker for the single in-flight transient-status auto-hide timer.
        # Every transient sticky status (``/cost-inline on``, ``/copy …``,
        # ``/docs-filter …``, etc.) arms ``app.set_timer(_, conv.hide_status)``
        # to auto-clear after ~2 s. Before this tracker, every arming created
        # a NEW timer and the previous one kept ticking — so a user firing two
        # transients in quick succession (or transient → live ``⟳ thinking…``)
        # would have the old timer silently hide the LIVE indicator a couple
        # of seconds later. Holding a single handle lets us cancel the prior
        # timer before installing a new one, and cancel it outright when a
        # live thinking-status arrives so it can't kill the agent's spinner.
        self._transient_status_timer = None  # type: ignore[assignment]
        # Dispatch table — each entry maps a `msg.kind` to its handler.
        # Methods on `self` are bound, so we can reference them directly.
        self.HANDLERS: dict[str, Callable[..., str | None]] = {
            "__end__":                  self._on_end,
            "__quit__":                 self._on_quit,
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
            "hot_list_updated":         self._on_hot_list_updated,
            # Issue #427 step 4: tool-call lifecycle from ChatEventForwarder
            # (= step 3). Mount a ToolCallRow on _started, finalise
            # on _completed / _failed, keyed by op_id.
            "tool_call_started":        self._on_tool_call_started,
            "tool_call_completed":      self._on_tool_call_completed,
            "tool_call_failed":         self._on_tool_call_failed,
        }

    # ── transient sticky helpers ──────────────────────────────────────────────

    def _cancel_transient_timer(self) -> None:
        """Cancel any in-flight auto-hide timer for the transient sticky.

        Called before arming a new one, and whenever a live thinking
        indicator takes over the sticky (= we must not allow the old
        auto-hide to fire and kill the live spinner).
        """
        timer = self._transient_status_timer
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            pass
        self._transient_status_timer = None

    def _show_transient_status(
        self,
        conv: ConversationView,
        text: str,
        *,
        kind: str = "general",
        duration: float = 2.5,
    ) -> None:
        """Show a transient sticky status that auto-hides after ``duration`` s.

        Single entry point for ``show + set_timer(hide)`` so the previous
        timer (if any) is always cancelled first. The previous behaviour
        of arming a fresh timer per call meant a transient fired right
        before an agent reply would hide the live ``⟳ thinking…`` ~2 s
        into the agent's run.
        """
        self._cancel_transient_timer()
        conv.show_status(text, kind=kind)
        self._transient_status_timer = self._app.set_timer(duration, conv.hide_status)

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

    def _on_quit(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__quit__` — /quit or /exit slash; trigger the same shutdown
        path as Ctrl+D.

        Wave-2 finding P3: the slash command palette previously had no
        ``/quit`` / ``/exit`` entry because the historical implementation
        intercepted them outside the registry. Routing through the
        sentinel here keeps the shutdown semantics identical to the
        Ctrl+D path (= the App's existing ``action_quit_tui`` does the
        graceful teardown + WAL flush).
        """
        self._app.call_later(self._app.action_quit_tui)

    def _on_attach_request(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__attach_request__` — agent switched; refresh header label.

        Also clears the sticky status: a ``⟳ thinking…`` left by the
        previous agent's in-flight turn would otherwise persist with
        the new agent's name attached in the header, confusing the user
        about WHICH agent is actively running. Any pending transient
        auto-hide timer is cancelled too — the new agent should not
        inherit a ghost timer from the old one's flow.

        Additionally clears the conversation log (= ``conv.clear()``)
        and writes a dim divider line naming the freshly attached
        agent. Previously only the header label updated, leaving the
        old agent's transcript fully visible — visually two agents'
        conversations blended into one scroll history and the user
        couldn't tell where one ended and the next began.
        """
        app = self._app
        new_name = msg.text
        if new_name and app._agent_registry is not None:
            app._agent_name = new_name
            header.refresh_status(agent_name=new_name)
            self._cancel_transient_timer()
            conv.hide_status()
            # Capture the running-skill identities BEFORE conv.clear() so
            # we can leave a breadcrumb after the wipe. Without this, a
            # hot-swap mid-skill silently discards the running row — the
            # user types ``/attach <other>`` and sees only ``── attached
            # to <other> ──``, with no trace of the in-flight skill that
            # just got cancelled. ``_skill_name`` / ``_short_id`` are
            # SkillActivityRow's stable display identifiers; reading them
            # here is intra-package coupling, but the alternative (a new
            # public accessor) is more surface area for the same purpose.
            interrupted: list[str] = []
            for row in list(conv._skill_rows.values()):
                try:
                    interrupted.append(
                        f"✗ {row._skill_name}#{row._short_id}"
                        " — interrupted by /attach"
                    )
                except Exception:
                    pass
            conv.clear()
            from rich.text import Text as _RichText
            for line in interrupted:
                conv._write_log(_RichText(line, style="dim #888888"))
            conv._write_log(_RichText(
                f"── attached to {new_name} ──",
                style="dim #555555",
            ))

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
        status indicating the new state. F10: also persists the new
        state to ``.reyn/tui_prefs.json`` so the next ``reyn chat``
        starts with the user's last choice instead of always reverting
        to off.
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
        self._show_transient_status(conv, f"cost-inline {state}", duration=2.5)
        # Persist the state-change in the conv log too — a 2.5 s sticky
        # is easy to miss if the user just scanned the screen. The
        # lifecycle-marker shape (= same dim ``── ↑ <body> ────`` divider
        # used by compaction markers) keeps the visual weight low while
        # leaving an auditable record of when the toggle fired.
        from reyn.chat.tui.widgets.conversation import _render_lifecycle_marker
        try:
            conv._write_log(_render_lifecycle_marker(f"↑ cost-inline {state}"))
        except Exception:
            pass
        # Persist the new state — additive merge, so unknown future
        # pref keys round-trip untouched. Failure is silent (= file
        # write errors don't break the toggle itself).
        from reyn.chat.tui.prefs import load_tui_prefs, save_tui_prefs
        root = app._project_root_path()
        if root is not None:
            prefs = load_tui_prefs(root)
            prefs["cost_inline"] = app._cost_inline_enabled
            save_tui_prefs(root, prefs)

    def _on_expand_last_reply(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__expand_last_reply__` — /expand slash; flush truncated reply."""
        if not conv.expand_last_reply():
            self._show_transient_status(conv, "nothing to expand", duration=2.0)

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
                self._show_transient_status(conv, "no replies buffered yet")
            else:
                self._show_transient_status(
                    conv,
                    f"{count} reply{'s' if count != 1 else ''} buffered "
                    f"— /copy 1..{count}",
                )
            return

        # Parse N. Empty → latest (n=1); digits → that index. Anything else
        # is a typo — surface the usage hint rather than silently copying
        # the latest, since that would mask the user's mistake.
        if arg == "":
            n = 1
        elif arg.isdigit():
            n = int(arg)
            if n <= 0:
                self._show_transient_status(
                    conv, "/copy index must be ≥ 1 (1 = newest)", kind="error",
                )
                return
        else:
            self._show_transient_status(
                conv, "usage: /copy [N] or /copy list", kind="error",
            )
            return

        text = conv.reply_at(n)
        if text is None:
            buffered = conv.recent_reply_count()
            if buffered == 0:
                self._show_transient_status(conv, "nothing to copy yet")
            else:
                self._show_transient_status(
                    conv,
                    f"reply {n} not in buffer "
                    f"(only {buffered} available — /copy list)",
                    kind="error",
                )
            return

        # Off-load the blocking ``subprocess.run`` (up to 2 s per tool tried)
        # to a thread executor so the outbox loop stays free to drain other
        # events. Without this, ``/copy`` mid-stream could freeze the TUI
        # for up to 2 s — streaming chunks would back up in the queue and
        # all unblock in a burst at the end. Surface an instant "copying…"
        # placeholder so the user sees their action register immediately;
        # the worker overwrites it with the success / failure status on
        # completion (each ``_show_transient_status`` cancels the prior
        # auto-hide timer, so the placeholder never out-lives the result).
        self._show_transient_status(conv, "copying…", duration=10.0)
        asyncio.create_task(self._finish_copy_async(conv, text, n))

    async def _finish_copy_async(
        self, conv: ConversationView, text: str, n: int,
    ) -> None:
        """Background worker for /copy — runs subprocess off the event loop."""
        from ._clipboard import copy_to_clipboard_async
        try:
            ok, label = await copy_to_clipboard_async(text)
        except Exception as exc:
            self._show_transient_status(
                conv, f"copy failed: {exc}", kind="error",
            )
            return
        if ok:
            n_chars = len(text)
            tag = "latest" if n == 1 else f"#{n}"
            self._show_transient_status(
                conv, f"copied reply {tag} ({n_chars} chars) via {label}",
            )
        else:
            self._show_transient_status(
                conv,
                "no clipboard tool found "
                "(install pbcopy / xclip / wl-copy / xsel)",
                kind="error",
            )

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
        self._show_transient_status(conv, msg_text, duration=2.0)

    def _on_stream_start(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__stream_start__` — begin a streaming agent reply row.

        The row is created keyed on the START message's ``msg_id``. Chunks
        and end events route by their own ``msg.meta["msg_id"]`` (see
        ``_on_stream_chunk`` / ``_on_stream_end``), NOT by a shared
        ``app._current_stream_id`` — that single-slot global was racy under
        concurrent streams (e.g. ``/attach`` mid-stream): a new start would
        overwrite it, and late chunks from the previous stream then
        appended to the wrong agent's row.
        """
        app = self._app
        msg_id = msg.meta.get("msg_id", id(msg))
        app._current_stream_id = msg_id
        # Hide the "thinking…" sticky now that the reply is starting. Cancel
        # any pending transient timer too — a transient that fired right
        # before the reply must not auto-hide a fresh sticky armed later
        # in this same turn.
        self._cancel_transient_timer()
        conv.hide_status()
        conv.begin_stream(msg_id, app._agent_name)

    def _on_stream_chunk(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__stream_chunk__` — append text to the row keyed on the chunk's own msg_id.

        Routing by ``msg.meta["msg_id"]`` (not the global
        ``app._current_stream_id``) means a late chunk from an earlier
        stream lands on its original row even if a newer stream has
        started in the meantime — the canonical fix for chunks bleeding
        across agents after a ``/attach`` mid-stream.
        """
        msg_id = msg.meta.get("msg_id")
        if msg_id is not None:
            conv.append_stream(msg_id, msg.text)

    def _on_stream_end(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`__stream_end__` — seal the row keyed on the end message's msg_id."""
        app = self._app
        msg_id = msg.meta.get("msg_id")
        if msg_id is not None:
            conv.end_stream(msg_id)
            # Only clear the "latest stream" pointer if THIS stream was
            # the latest. If a newer stream started in between, its id
            # is still the latest in-flight and must stay tracked.
            if app._current_stream_id == msg_id:
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
        #
        # Issue #276 Phase B (3/5): in ``--connect`` mode the proxy's
        # ``_interventions`` is None, so the registry-based path below
        # collapses to 0 even when the remote server has multiple
        # queued interventions. The WS server inlines ``queued_count``
        # into the intervention frame's meta (= ``web/ws/chat.py``
        # ``_serialize`` augmentation) — read from there as a fallback
        # so the ``+N more pending`` badge stays accurate in remote
        # mode.
        queued_extra = 0
        try:
            session = self._app._get_session()
            registry = getattr(session, "_interventions", None) if session else None
            if registry is not None:
                queued_extra = max(0, registry.queued_count() - 1)
            else:
                # Remote-mode fallback — meta.queued_count was set by
                # the WS server forwarder.
                meta_count = msg.meta.get("queued_count")
                if isinstance(meta_count, int):
                    queued_extra = max(0, meta_count - 1)
        except Exception:
            queued_extra = 0
        # Issue #163: prefer the structured ``prompt`` from meta over the
        # concatenated ``msg.text`` so the widget renders just the
        # question (= "Permission request — web.fetch") without the
        # bolted-on detail / choices lines. ``meta.detail`` is forwarded
        # as a separate Label with italic-muted styling.  Old emit sites
        # that don't set meta.prompt fall back to msg.text — backward-
        # compat for the CLI Panel path and any legacy producers.
        question_text = str(msg.meta.get("prompt") or msg.text)
        detail_text = msg.meta.get("detail")
        # Issue #261 — opt-in source_agent stamped by ChatSession's
        # parent_delegate branch (see services/intervention_handler.py
        # ``source_agent_var``). Absent on the default user_channel
        # path, so the existing non-delegated flow is unaffected.
        source_agent = msg.meta.get("source_agent")
        self._app._mount_intervention(
            conv,
            question_text,
            iv_id,
            choices,
            queued_extra=queued_extra,
            detail=str(detail_text) if detail_text else None,
            source_agent=str(source_agent) if source_agent else None,
        )
        # Persistent "blocked on user" indicator. The InterventionWidget is
        # mounted inline and can scroll off-screen in a long session; the
        # sticky stays at the bottom of the conv pane so the user can't
        # lose the "agent is waiting for me" signal by scrolling up to
        # review prior turns. Shown AFTER ``_mount_intervention`` because
        # ``conv.mount_intervention`` internally calls ``hide_status`` to
        # clear any prior thinking spinner — showing before that would be
        # silently undone. Cleared on resolution (_on_intervention_resolved).
        # General-kind so no ⟳ spinner / elapsed timer appears — the agent
        # is paused, not working.
        try:
            conv.show_status("⚑ awaiting your answer", kind="general")
        except Exception:
            pass
        # Out-of-band signals: the agent is hard-blocked on a human answer,
        # so a user in a background terminal tab needs to know to come
        # back. The terminal title flips visible in the tab bar; the BEL
        # gives an audio cue. Both reset when the user submits an answer
        # (see InputBar.UserSubmitted path → set_title_state(None)).
        self._app.set_title_state("awaiting answer")
        self._app.alert()

    def _on_intervention_resolved(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`intervention_resolved` — remove the inline widget when the user
        answered via text input (not a chip button click).

        The chip-button path calls ``InterventionWidget._submit`` which already
        calls ``self.remove()``.  The text-input path (Enter in the InputBar)
        routes through session._deliver_answer_to without touching the widget,
        so we need this outbox message to clean up the orphaned widget.

        Also clears the "⚑ awaiting your answer" sticky set by
        ``_on_intervention`` — both resolution paths flow through this
        outbox message, so it's the single dismissal point.
        """
        try:
            conv.hide_status()
        except Exception:
            pass
        # Wave-9 E-F1: accept either ``intervention_id`` (= the key used
        # by ``_on_intervention`` and by the service's mount + cancel
        # emits) or ``iv_id`` (= the legacy key used by the resolved
        # emit only). Reading both with ``intervention_id`` preferred
        # makes the TUI immune to which key any future producer chooses
        # — without this, a one-line rename on the service side would
        # silently leak InterventionWidget orphans (= the lookup at
        # ``query_one(#iv_…)`` would miss, ``except Exception: pass``
        # would swallow it, and the widget would stay mounted with no
        # way to remove it). Same id field, two historical names,
        # picked here in canonical-key order.
        iv_id = msg.meta.get("intervention_id") or msg.meta.get("iv_id", "")
        widget_id = f"iv_{iv_id[:8]}"
        try:
            widget = self._app.query_one(f"#{widget_id}")
            widget.remove()
        except Exception:
            pass
        # Mirror InterventionWidget._submit's focus restoration. The
        # text-input path resolves the intervention from the InputBar's
        # ``on_submitted`` handler so the user's focus is already there
        # in the common case — but if the user clicked into the panel or
        # tabbed away while waiting for confirmation, the widget removal
        # would otherwise leak focus to a non-editable peer.
        try:
            from .widgets import InputBar
            self._app.query_one("#inputbar", InputBar).focus_input()
        except Exception:
            pass

    def _on_status(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`status` — route to sticky bar (not the log).

        A live ``⟳ thinking…`` must outlive any pending transient timer
        from a slash command that fired just before this turn started —
        otherwise the auto-hide kills the agent's spinner mid-thought.

        Priority guard: ``planner.py`` and ``router_loop.py`` both emit
        ``kind="status"`` messages that flow through the same sticky bar.
        Without a guard, a router-loop "dispatched N async requests"
        update arrives mid-plan and overwrites the more-informative
        "plan step N/M: <desc>" counter — the user briefly loses the
        per-step location they just saw and the elapsed timer resets
        each swap.

        ``planner.py`` already tags its emissions with
        ``meta={"source": "plan", ...}``; the router-loop "dispatched"
        path does not. When the incoming message lacks that source tag
        AND the currently-displayed sticky body matches a plan-shaped
        prefix (``plan step``, ``plan started``, or the retry banner
        ``リトライ``), the lower-priority dispatch update is dropped.
        Plan-sourced updates always go through, so step counters
        continue to advance normally.
        """
        self._cancel_transient_timer()
        incoming_source = (msg.meta or {}).get("source")
        if incoming_source != "plan":
            try:
                snap = conv._sticky().snapshot() if conv._sticky() else None
            except Exception:
                snap = None
            if snap and snap.get("active"):
                current_body = str(snap.get("body", ""))
                if _is_plan_sourced_body(current_body):
                    return  # don't overwrite the higher-priority plan status
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
        # Out-of-band: flag the terminal title and ring the bell so a
        # user with reyn in a background tab notices something failed.
        # Reset on the next user submit.
        self._app.set_title_state("error")
        self._app.alert()

    # ── Tool-call lifecycle (issue #427 step 4) ───────────────────────────────

    def _on_tool_call_started(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """``tool_call_started`` — mount a ToolCallRow for this op_id.

        ``meta`` carries the dispatcher-side payload bridged by
        ``ChatEventForwarder.on_tool_called``:
            {tool, op_id, args, chain_id, run_id, ...}

        Render ``args`` as a compact ``key=value`` line (= the same shape
        ToolCallRow's truncation helper expects). The conv pane keys
        the row off ``op_id`` so the eventual completed / failed
        message can find it back.
        """
        meta = msg.meta or {}
        op_id = str(meta.get("op_id") or "")
        tool_name = str(meta.get("tool") or msg.text or "tool")
        args = meta.get("args") or {}
        # F-F: pass through the source run_id so the conv pane can nest
        # the tool_call row under its owning SkillActivityRow when one
        # is currently mounted (= sub-skill spawned the call). The conv
        # pane checks whether the run_id matches a mounted skill row;
        # unmatched → root-level rendering (no prefix).
        parent_run_id = str(meta.get("run_id") or "")
        try:
            conv.start_tool_call_row(
                op_id,
                tool_name,
                args_repr=_format_tool_args(args),
                parent_run_id=parent_run_id,
            )
        except Exception:
            pass

    def _on_tool_call_completed(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """``tool_call_completed`` — finalise the row to its success terminal."""
        meta = msg.meta or {}
        op_id = str(meta.get("op_id") or "")
        result = meta.get("result")
        try:
            conv.complete_tool_call_row(
                op_id, result_snippet=_format_tool_result(result),
            )
        except Exception:
            pass

    def _on_tool_call_failed(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """``tool_call_failed`` — finalise the row to its failure terminal."""
        meta = msg.meta or {}
        op_id = str(meta.get("op_id") or "")
        error_kind = str(meta.get("error_kind") or "")
        error_msg = str(meta.get("error_message") or "")
        # Prefer "kind: message" when both present; fall back to whichever
        # one is non-empty. Keeps the row's error display informative even
        # when the dispatcher omits one of the two fields.
        if error_kind and error_msg:
            reason = f"{error_kind}: {error_msg}"
        else:
            reason = error_kind or error_msg or "failed"
        try:
            conv.fail_tool_call_row(op_id, error=reason)
        except Exception:
            pass

    def _on_hot_list_updated(
        self, msg: OutboxMessage, conv: ConversationView, header: ReynHeader,
    ) -> None:
        """`hot_list_updated` — refresh the app's cached hot-list ranking.

        The forwarder emits this whenever the qualified-name **order**
        changes (= ``ActionUsageTracker.record()`` detects a top-N diff;
        score-only bumps within a stable order are suppressed upstream).
        The Memory tab reads the cached ranking when it next paints
        ("Hot now" sub-section), so we trigger a panel refresh here
        instead of waiting for the user to switch tabs.

        ``meta["ranking"]`` is a list of dicts
        ``[{"qualified_name": str, "freq": int, "last_ts": str}, ...]``,
        full ranking (= not top-N). Missing / malformed → empty list.
        """
        raw = msg.meta.get("ranking") if msg.meta else None
        ranking = list(raw) if isinstance(raw, list) else []
        # Push to the right panel's cache. ``update_hot_list`` triggers
        # an invalidate only when the Memory tab is currently visible —
        # other tabs see the new ranking the next time the user
        # switches to Memory (state is already cached on the panel).
        try:
            from .widgets import RightPanel
            self._app.query_one(
                "#right_panel", RightPanel,
            ).update_hot_list(ranking)
        except Exception:
            pass


__all__ = ["OutboxRouter"]
