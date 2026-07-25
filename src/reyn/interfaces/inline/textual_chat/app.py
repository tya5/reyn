"""The :class:`TextualChatApp` and its :func:`run_textual_chat` entry point.

The app OWNS both input (a :class:`~reyn.interfaces.inline.textual_chat.chrome.Composer`)
and output (a retained :class:`~textual_flowview.FlowModel` rendered through a
:class:`~textual_flowview.FlowView`), fed from the SAME ``transport.frames()``
stream the plain output loop consumes: a Textual worker drains the stream and
appends each display frame to the model, so the on-screen turn sequence is
structurally identical to the plain renderer's — only the drawing differs. Body
presentation is the
:class:`~reyn.interfaces.inline.textual_chat.presenter.ReynPresenter`'s job, the
state-coloured gutter the
:class:`~reyn.interfaces.inline.textual_chat.gutter.ReynGutter`'s, and the bottom
chrome the widgets in :mod:`~reyn.interfaces.inline.textual_chat.chrome`. This app
wires them, drives the frame pump + blink timer, and routes composer submissions
back through the transport send seam.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` / ``textual_flowview``
imports never reach an always-loaded module.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, OptionList, Static, Tab
from textual_flowview import (
    Anchor,
    Entry,
    EntryState,
    FlowModel,
    FlowView,
)

from reyn.interfaces.repl.renderer import summarize_tool_result
from reyn.interfaces.transport.frames import FrameTag

from .chrome import _MENU_TABS, Composer, MenuBar, StatusLine, _drawer_child
from .gutter import ReynGutter
from .presenter import ReynPresenter

if TYPE_CHECKING:
    from textual.timer import Timer

    from reyn.interfaces.repl.read_model import ChatReadModel
    from reyn.interfaces.transport.client_transport import ClientTransport
    from reyn.runtime.outbox import OutboxMessage

logger = logging.getLogger(__name__)

# Display kinds that are command-UI / control sentinels, not conversation
# content. The plain output loop consumes them as signals; the Phase-1
# conversation pane simply skips them (their surfaces land in later phases).
# ``__end__`` is handled by the pump loop (it stops the app), so it is not here.
_SKIP_KINDS = frozenset(
    {
        "__copy_last_reply__",
        "__rewind_list__",
        "__attach_request__",
        "__session_switch_request__",
    }
)


class TextualChatApp(App):
    """The TTY conversation pane: a FlowView of the live conversation + a
    Composer, both fed/served by one :class:`ClientTransport`.

    The app drains ``transport.frames()`` in a worker, appending each display
    frame to the retained model (event frames are consumed but not yet drawn);
    a Composer submit routes back through the transport. The user's own line is
    NOT echoed locally — it returns as a ``kind="user"`` frame on the same
    stream, so the model is fed entirely from frames and stays equivalent to the
    plain renderer's turn sequence.

    Phase 2 adds state-coloured gutters: a tool-call row transitions RUNNING →
    SUCCESS/ERROR (amber → green/coral) as its correlated frames arrive, a
    failed row is tinted coral edge-to-edge, and RUNNING rows blink via an
    app-side timer (:meth:`_advance_blink`). The blink is additive — neutering
    the timer leaves a static, correct gutter.

    Phase 3 adds the bottom-chrome tab-drawer: below the composer, a
    :class:`StatusLine` + a focusable :class:`MenuBar`, and a
    :class:`~textual.widgets.ContentSwitcher` drawer that is collapsed by default
    and expands DOWNWARD when a menu item is opened (see :meth:`_open_drawer`).
    The drawer content is placeholder — real registry wiring is Phase 4.
    """

    #: Seconds between running-blink frames (app-side; textual-flowview unmodified).
    BLINK_INTERVAL = 0.5

    BINDINGS = [
        # Global fallback so Esc closes the drawer even when focus is INSIDE it
        # (an OptionList pane); the MenuBar's own ↑/Esc handles the menu-row case.
        ("escape", "close_drawer", "Close drawer"),
    ]

    CSS = """
    Screen { layout: vertical; }
    FlowView {
        height: 1fr;
        scrollbar-size-vertical: 0;
    }
    #inputrow {
        height: auto;
        max-height: 8;
        margin-top: 1;
        border-top: solid #3d434f;
        border-bottom: solid #3d434f;
    }
    #inputgutter {
        width: 2;
        height: auto;
        color: $text-muted;
    }
    Composer {
        height: 3;
        max-height: 6;
        border: none;
        padding: 0;
    }
    StatusLine {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    MenuBar {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    MenuBar:focus-within { color: $text; }
    MenuBar Tab { padding: 0 1; }
    /* No separator rule between the menu row and its drawer — they read as one
       continuous, edge-to-edge block (the $panel background is the only cue). */
    #drawer {
        height: auto;
        max-height: 12;
        background: $panel;
        padding: 0;
    }
    /* OptionList ships an all-round default border — strip it so the drawer
       content is edge-to-edge (full-width highlight rows, no side frame). */
    #drawer OptionList {
        height: auto;
        max-height: 12;
        background: $panel;
        border: none;
        padding: 0;
    }
    #drawer Static { height: auto; padding: 1 0; }
    """

    def __init__(
        self,
        *,
        transport: "ClientTransport",
        read_model: "ChatReadModel | None" = None,
        agent_name: str = "default",
        config=None,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._read_model = read_model
        self._agent_name = agent_name
        self._config = config
        self.conversation: "FlowModel[OutboxMessage]" = FlowModel()
        # Running tool-call entries keyed by op_id (== the dispatcher's
        # deterministic args_hash, meta["op_id"]) so a later completion/failure
        # frame transitions the SAME entry RUNNING → SUCCESS/ERROR (CC parity).
        self._running_tools: "dict[object, Entry[OutboxMessage]]" = {}
        # Shared blink frame counter read by ReynGutter; advanced by the timer.
        self._blink_count = 0
        self._blink_timer: "Timer | None" = None

    def compose(self) -> ComposeResult:
        yield FlowView(
            model=self.conversation,
            presenter=ReynPresenter(),
            decorator=ReynGutter(blink_frame=lambda: self._blink_count),
            gutter_width=2,
            spacing=1,
            anchor=Anchor.STICKY_BOTTOM,
        )
        with Horizontal(id="inputrow"):
            yield Static("❯", id="inputgutter")
            yield Composer(
                placeholder="Type a message — Enter to send, Shift+Enter for a newline…"
            )
        # Bottom chrome (Phase 3): a slim status-values line + a focusable menu
        # row, then a drawer (ContentSwitcher) that stays collapsed until a menu
        # item opens it downward. Content is placeholder (Phase 4 wires the data).
        yield StatusLine(self._status_text())
        yield MenuBar(*(Tab(label, id=tid) for tid, label in _MENU_TABS), id="menubar")
        with ContentSwitcher(initial=None, id="drawer"):
            for tid, _label in _MENU_TABS:
                yield _drawer_child(tid)

    def _status_text(self) -> str:
        """The status-values line (``model │ agent │ cost │ ctx``). PLACEHOLDER
        values in Phase 3 — Phase 4 sources them from reyn's cost/token trackers
        and the model/agent selection. The agent name is the one already threaded
        into the app so at least that value is live."""
        return f"model sonnet │ agent {self._agent_name} │ cost $0.0000 │ ctx 0%"

    def on_mount(self) -> None:
        # The running-blink timer starts PAUSED and is resumed only while a
        # tool call is RUNNING (and paused again when none remain) — it never
        # spins on an idle conversation. The blink is app-side + ADDITIVE:
        # neutering ``_advance_blink`` freezes the frame to a static gutter
        # without affecting correctness (see the Phase-2 strip gate).
        self._blink_timer = self.set_interval(
            self.BLINK_INTERVAL, self._advance_blink, pause=True
        )
        self.run_worker(self._pump_frames(), name="frames", exclusive=True)
        # Drawer starts collapsed — the default chrome is just the two slim rows
        # (status-values line + menu row). It only becomes visible when a menu
        # item is opened (:meth:`_open_drawer`).
        self.query_one("#drawer", ContentSwitcher).display = False
        self.query_one(Composer).focus()

    def _open_drawer(self, tab_id: "str | None") -> None:
        """Expand/collapse the downward drawer. ``None`` (or the ``"__close__"``
        sentinel) collapses it and returns focus to the composer; a tab id shows
        that pane, focusing the :class:`OptionList` when the pane is an
        interactive picker so ``↑``/``↓`` immediately drive the selection."""
        drawer = self.query_one("#drawer", ContentSwitcher)
        if tab_id is None or tab_id == "__close__":
            drawer.display = False
            drawer.current = None
            self.query_one(Composer).focus()
            return
        drawer.current = tab_id
        drawer.display = True
        child = drawer.query_one(f"#{tab_id}")
        if isinstance(child, OptionList):
            child.focus()

    def on_menu_bar_selected(self, event: "MenuBar.Selected") -> None:
        self._open_drawer(None if event.tab_id == "__close__" else event.tab_id)

    def on_option_list_option_selected(self, event: "OptionList.OptionSelected") -> None:
        # A real impl (Phase 4) applies the picked model/agent/etc.; Phase 3 just
        # collapses back to the composer.
        self._open_drawer(None)

    def action_close_drawer(self) -> None:
        self._open_drawer(None)

    def _advance_blink(self) -> None:
        """One blink tick: advance the shared frame counter and redraw ONLY the
        running entries' gutters (``set_metadata`` is flowview's gutter-only
        redraw primitive — it never re-presents the body). Pauses itself when no
        entry is RUNNING, so the timer does not spin on an idle conversation."""
        self._blink_count += 1
        for entry in list(self._running_tools.values()):
            entry.set_metadata("_blink", self._blink_count)
        if not self._running_tools and self._blink_timer is not None:
            self._blink_timer.pause()

    def _track_tool_state(self, msg: "OutboxMessage", entry: "Entry[OutboxMessage]") -> None:
        """Drive the Phase-2 lifecycle state of tool-call / error rows.

        A ``tool_call_started`` row (with an ``op_id`` correlation key) becomes
        RUNNING and starts blinking; the matching ``tool_call_completed`` /
        ``tool_call_failed`` frame transitions that SAME entry to SUCCESS/ERROR
        (its gutter goes amber → green/coral). ``error`` rows go straight to
        ERROR. Frames without an ``op_id`` carry no state (DEFAULT) — they append
        exactly as in Phase 1, so the plain-fallback turn sequence is unchanged."""
        kind = msg.kind
        meta = msg.meta or {}
        op_id = meta.get("op_id")
        if kind == "tool_call_started":
            if op_id is not None:
                entry.set_state(EntryState.RUNNING)
                self._running_tools[op_id] = entry
                if self._blink_timer is not None:
                    self._blink_timer.resume()
        elif kind == "tool_call_completed":
            summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
            started = self._running_tools.pop(op_id, None) if op_id is not None else None
            if started is not None:
                started.set_state(
                    EntryState.ERROR if summary.startswith("✗") else EntryState.SUCCESS
                )
            self._maybe_pause_blink()
        elif kind == "tool_call_failed":
            started = self._running_tools.pop(op_id, None) if op_id is not None else None
            if started is not None:
                started.set_state(EntryState.ERROR)
            entry.set_state(EntryState.ERROR)
            self._maybe_pause_blink()
        elif kind == "error":
            entry.set_state(EntryState.ERROR)

    def _maybe_pause_blink(self) -> None:
        if not self._running_tools and self._blink_timer is not None:
            self._blink_timer.pause()

    async def _pump_frames(self) -> None:
        """Drain the transport frame stream into the retained model.

        Display frames append to the model (skipping command-UI sentinels);
        ``__end__`` stops the app (the session closed). Event frames are the
        working-indicator path — consumed but not yet drawn. Each appended entry
        is then handed to :meth:`_track_tool_state` for its Phase-2 lifecycle
        colour. A single frame's presentation failure must not kill the pump, so
        append is guarded.
        """
        try:
            async for frame in self._transport.frames():
                if frame.tag is FrameTag.EVENT:
                    continue
                msg = frame.message
                if msg.kind == "__end__":
                    break
                if msg.kind in _SKIP_KINDS:
                    continue
                try:
                    entry = self.conversation.append(msg)
                except Exception:
                    logger.exception(
                        "textual chat: append failed for frame kind=%r", msg.kind
                    )
                    continue
                try:
                    self._track_tool_state(msg, entry)
                except Exception:
                    logger.exception(
                        "textual chat: state tracking failed for kind=%r", msg.kind
                    )
        finally:
            self.exit()

    async def on_composer_submitted(self, event: "Composer.Submitted") -> None:
        text = event.value.strip()
        self.query_one(Composer).clear_and_reset()
        if not text:
            return
        if text in {"/quit", "/exit"}:
            await self._transport.shutdown()
            self.exit()
            return
        await self._submit(text)

    async def _submit(self, text: str) -> None:
        """Route one submitted line through the transport send seam.

        A pending free-text intervention (no choices) takes the answer path;
        everything else is an ordinary new turn. Errors are contained and
        surfaced as an error frame the pump renders — a silent input drop is the
        worst failure for a chat box.
        """
        try:
            head = self._transport.pending_intervention_head()
            if head is not None and not getattr(head, "choices", None):
                await self._transport.answer_intervention_text(text)
                return
            await self._transport.submit_user_text(text)
        except Exception as exc:
            logger.exception("textual chat: submit failed")
            from reyn.runtime.outbox import OutboxMessage
            detail = f"{type(exc).__name__}: {exc}"
            try:
                self._transport.put_display(
                    OutboxMessage(
                        kind="error", text=f"input could not be submitted: {detail}"
                    )
                )
            except Exception:
                pass


async def run_textual_chat(
    *,
    transport: "ClientTransport",
    read_model: "ChatReadModel | None" = None,
    agent_name: str = "default",
    config=None,
) -> None:
    """Run the TTY conversation-pane app until the user quits or the stream ends.

    Runs ``inline=True`` so the terminal's pre-launch scrollback is preserved
    above the app region (the ADR's Phase-0-validated inline mode) rather than
    swapping to the alternate screen. Returns so the driver's caller can tear the
    transport down + print the cost summary.
    """
    app = TextualChatApp(
        transport=transport,
        read_model=read_model,
        agent_name=agent_name,
        config=config,
    )
    await app.run_async(inline=True)
