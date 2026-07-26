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
wires them, drives the frame pump, and routes composer submissions back through
the transport send seam. The running-blink gutter animates itself off
textual-flowview's native ``FlowView(animation_fps=N)`` clock — there is no
app-side blink timer.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` / ``textual_flowview``
imports never reach an always-loaded module.
"""
from __future__ import annotations

import logging
from dataclasses import replace
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

from .chrome import (
    _MENU_TABS,
    Composer,
    MenuBar,
    StatusLine,
    build_drawer_pane,
    pane_payload,
    status_line_text,
)
from .gutter import _RUNNING_FRAME_PERIOD, ReynGutter
from .presenter import ReynPresenter, choice_chip_spans
from .restore import project_restored_frames

if TYPE_CHECKING:
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

#: FlowView gutter column width (state-coloured marker). Shared by ``compose``
#: (the ``FlowView(gutter_width=…)`` config) and the choice-chip click handler,
#: which must reconstruct the presenter's body width (content − gutter) to know
#: which ``event.x`` column a click landed on.
_GUTTER_WIDTH = 2

#: Sentinel for :meth:`TextualChatApp._pane_rows`'s optional ``snap`` argument —
#: distinguishes "no snapshot passed, read a fresh one" from an explicit ``None``
#: snapshot (pre-session), which must NOT trigger a second read.
_UNSET: object = object()


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
    failed row is tinted coral edge-to-edge, and RUNNING rows blink. The blink is
    NATIVE: ``FlowView(animation_fps=N)`` re-invokes the time-based
    :class:`ReynGutter` decorator on each animation tick, and the decorator picks
    the frame from a monotonic clock — no app-side timer, no shared counter. The
    blink is additive: a frozen clock (``frame_period<=0``) leaves a static,
    correct amber gutter.

    Phase 3 adds the bottom-chrome tab-drawer: below the composer, a
    :class:`StatusLine` + a focusable :class:`MenuBar`, and a
    :class:`~textual.widgets.ContentSwitcher` drawer that is collapsed by default
    and expands DOWNWARD when a menu item is opened (see :meth:`_open_drawer`).

    Phase 4 wires each drawer pane to its canonical reyn source, rebuilding the
    pane from a fresh status snapshot (:meth:`_snapshot`) on each open: Model/Agent
    from ``model_classes`` / ``agent_names`` (the enumerating pickers derive their
    FULL set from the registry, never a curated subset), Cost/Ctx from the live
    token/cost figures (F5b — also surfaced on the always-visible status line),
    Menu from the slash ``REGISTRY``, History from the retained live conversation,
    Help from the app BINDINGS. Selecting a Model/Agent row routes the equivalent
    ``/model`` / ``/attach`` slash through the transport.

    Phase 5 adds restore-on-restart (CC ``--resume`` parity): :meth:`on_mount`
    hydrates the retained model from the PERSISTED conversation log BEFORE the
    live frame pump starts (:meth:`_hydrate_from_history` →
    :func:`.restore.project_restored_frames`), reading the durable ``ChatMessage``
    log off the new ``ChatReadModel.conversation_history`` seam (``history.jsonl``,
    NOT the P6 audit-event log). Restored turns render RESOLVED (never RUNNING)
    through the same presenter/gutter path a live frame does, so a restart shows
    the previous conversation instead of a blank pane. The History drawer pane
    reads the same retained model, so it now shows those restored turns too. A
    REMOTE read model returns an empty log (frame-sufficiency: past turns are not
    on the wire) → hydration is a no-op and the pane starts blank as before.

    Phase 3.5 wires CHOICE interventions: a closed-set intervention (permission
    confirm / choice ``ask_user`` — any ``kind="intervention"`` frame carrying
    ``meta["choices"]``) is surfaced by the presenter as in-flow amber option
    chips; a click on a chip (:meth:`on_flow_view_clicked`) delivers that
    choice's id through ``transport.answer_intervention_choice`` and re-presents
    the entry to a green resolved state. Free-text interventions keep taking the
    composer answer path (:meth:`_submit`). This restores choice-intervention
    reachability, which the free-text-only wiring had left unanswerable in the
    Textual TTY.
    """

    #: FlowView animation frame rate (Hz) driving the native running-blink gutter.
    #: Set to ``1 / <blink frame period>`` so textual-flowview's own animation tick
    #: re-invokes the time-based :class:`ReynGutter` at least once per frame — the
    #: visible blink cadence matches the pre-native 0.5s app-side timer. ``fps>0``
    #: means the clock is always-on (no idle-pause); the idle tick is a viewport-
    #: gated gutter re-derive measured at ~0.003% of one core, so always-on is
    #: accepted over per-entry toggling (see #3283 ①).
    ANIMATION_FPS = 1.0 / _RUNNING_FRAME_PERIOD

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
        # One presenter instance, shared between the FlowView (which DRAWS entries)
        # and the choice-chip click handler (which asks it which body row the chips
        # landed on), so hit-testing measures the prompt head exactly as it was
        # drawn.
        self._presenter = ReynPresenter()
        # Running tool-call entries keyed by op_id (== the dispatcher's
        # deterministic args_hash, meta["op_id"]) so a later completion/failure
        # frame transitions the SAME entry RUNNING → SUCCESS/ERROR (CC parity).
        self._running_tools: "dict[object, Entry[OutboxMessage]]" = {}
        # Per-picker parallel id lists (class names / agent names), keyed by tab
        # id and kept in lock-step with the OptionList options a pane was last
        # refreshed with, so an ``OptionSelected.option_index`` maps back to the
        # canonical id the ``/model`` / ``/attach`` slash needs. Populated on each
        # drawer refresh (:meth:`_refresh_pane`) from the SAME snapshot that built
        # the rows, so the option row and its id never drift.
        self._pane_selection_ids: "dict[str, list[str]]" = {}

    def compose(self) -> ComposeResult:
        yield FlowView(
            model=self.conversation,
            presenter=self._presenter,
            decorator=ReynGutter(frame_period=_RUNNING_FRAME_PERIOD),
            gutter_width=_GUTTER_WIDTH,
            spacing=1,
            anchor=Anchor.STICKY_BOTTOM,
            # Native running-blink: FlowView owns the animation clock and
            # re-invokes the time-based ReynGutter each tick (no app-side timer).
            animation_fps=self.ANIMATION_FPS,
        )
        with Horizontal(id="inputrow"):
            yield Static("❯", id="inputgutter")
            yield Composer(
                placeholder="Type a message — Enter to send, Shift+Enter for a newline…"
            )
        # Bottom chrome: a slim status-values line + a focusable menu row, then a
        # drawer (ContentSwitcher) that stays collapsed until a menu item opens it
        # downward. Phase 4 fills each pane from its canonical reyn source; each
        # pane is rebuilt from a fresh snapshot when opened (:meth:`_refresh_pane`).
        yield StatusLine(self._status_text())
        yield MenuBar(*(Tab(label, id=tid) for tid, label in _MENU_TABS), id="menubar")
        with ContentSwitcher(initial=None, id="drawer"):
            for tid, _label in _MENU_TABS:
                yield build_drawer_pane(tid, self._pane_rows(tid))

    def _snapshot(self) -> "dict | None":
        """The live status snapshot (model/agent/cost/ctx) off the client read
        model — the SAME seam the plain path's status bar reads. ``None`` when
        there is no read model or no attached session yet (pre-session): every
        pure pane formatter degrades gracefully to empty/zero on ``None``, so the
        drawer never fabricates a value and the plain-fallback stays untouched."""
        if self._read_model is None:
            return None
        try:
            return self._read_model.snapshot(self._config)
        except Exception:
            logger.exception("textual chat: status snapshot read failed")
            return None

    def _app_binding_help(self) -> "list[tuple[str, str]]":
        """The app's declarative ``BINDINGS`` as ``(key, description)`` pairs for
        the Help pane — sourced from the binding table itself, not re-typed."""
        out: list[tuple[str, str]] = []
        for b in self.BINDINGS:
            if isinstance(b, tuple) and len(b) >= 3:
                out.append((b[0], b[2]))
        return out

    def _history_turns(self, *, limit: int = 12) -> "list[str]":
        """Recent conversation turns from the retained model — ``role · <first
        line>`` rows, newest ``limit``. The model holds BOTH the live frame
        stream the app drains AND (Phase 5) the restored prior turns
        :meth:`_hydrate_from_history` seeds at ``on_mount`` from ``history.jsonl``,
        so the History drawer is cross-session by construction: restoring the
        conversation into the model is what backs this pane's past-session view
        (no separate accessor call here — reading the model is sufficient once it
        is hydrated)."""
        rows: list[str] = []
        for entry in self.conversation:
            msg = entry.item
            if msg.kind not in ("user", "reply", "agent"):
                continue
            body = (msg.text or "").strip()
            head = body.splitlines()[0][:60] if body else ""
            role = "you" if msg.kind == "user" else "reyn"
            rows.append(f"{role} · {head}")
        return rows[-limit:]

    def _pane_rows(self, tab_id: str, snap: "dict | None | object" = _UNSET) -> "list[str]":
        """The display rows for ``tab_id``'s pane, derived from canonical sources:
        the status snapshot (model/agent/cost/ctx), the slash ``REGISTRY`` (menu),
        the live conversation (history), and the app BINDINGS (help). Pass ``snap``
        to reuse an already-read snapshot (keeps the rows and the selection ids
        derived from ONE snapshot)."""
        from reyn.interfaces.slash import REGISTRY  # noqa: PLC0415 — TTY-local
        snapshot = self._snapshot() if snap is _UNSET else snap
        return pane_payload(
            tab_id,
            snapshot=snapshot,  # type: ignore[arg-type]
            commands=REGISTRY.all_commands(),
            history=self._history_turns(),
            app_bindings=self._app_binding_help(),
        )

    def _selection_ids(self, tab_id: str, snap: "dict | None") -> "list[str]":
        """The canonical ids parallel to a picker pane's option rows (class names
        for Model, agent names for Agent), for mapping an ``option_index`` back to
        the ``/model`` / ``/attach`` argument. Empty for non-actionable panes."""
        s = snap or {}
        if tab_id == "model":
            return list(s.get("model_classes") or [])
        if tab_id == "agent":
            return list(s.get("agent_names") or [])
        return []

    def _status_text(self) -> str:
        """The status-values line (``model │ agent │ cost │ ctx``), from the live
        status snapshot (F5b: running cost + context percent are visible even with
        the drawer closed). Falls back to the threaded ``agent_name`` pre-session."""
        return status_line_text(self._snapshot(), self._agent_name)

    def on_mount(self) -> None:
        # Phase 5 (#3273): hydrate the retained model from the PERSISTED
        # conversation log BEFORE the live frame pump starts, so a restart shows
        # the previous conversation (CC ``--resume`` parity) instead of a blank
        # pane. Restored turns render resolved (never RUNNING) through the exact
        # same presenter/gutter path a live frame does. Must run before
        # ``run_worker`` so the prior turns sit ABOVE the first live frame.
        self._hydrate_from_history()
        # The running-blink gutter animates off FlowView's NATIVE animation clock
        # (``animation_fps`` wired in :meth:`compose`), not an app-side timer — so
        # there is nothing to start/pause here. The blink is ADDITIVE: a frozen
        # clock leaves a static, correct amber gutter (see the Phase-2 strip gate).
        self.run_worker(self._pump_frames(), name="frames", exclusive=True)
        # Drawer starts collapsed — the default chrome is just the two slim rows
        # (status-values line + menu row). It only becomes visible when a menu
        # item is opened (:meth:`_open_drawer`).
        self.query_one("#drawer", ContentSwitcher).display = False
        self.query_one(Composer).focus()

    def _hydrate_from_history(self) -> None:
        """Restore-on-restart (#3273 Phase 5): project the persisted conversation
        log into the retained model so a restart shows the PREVIOUS conversation.

        Reads the durable ``ChatMessage`` log via the read-model seam
        (:meth:`~reyn.interfaces.repl.read_model.ChatReadModel.conversation_history`
        — ``history.jsonl``, NOT the P6 audit-event log) and appends each projected
        frame to ``self.conversation`` (:func:`.restore.project_restored_frames`).
        Every restored entry is RESOLVED, never RUNNING: a completed tool result
        gets the SUCCESS/ERROR lifecycle state the live path's completion handler
        (:meth:`_track_tool_state`) would have assigned, and user/agent rows keep
        DEFAULT (their live state). A REMOTE read model returns an empty log
        (frame-sufficiency: past turns are not on the wire) → this is a no-op and
        the pane starts blank, exactly as before. Fully guarded — a restore
        failure must never stop the app from mounting and pumping live frames."""
        if self._read_model is None:
            return
        try:
            messages = self._read_model.conversation_history()
        except Exception:
            logger.exception("textual chat: conversation-history read failed")
            return
        try:
            frames = project_restored_frames(messages)
        except Exception:
            logger.exception("textual chat: history projection failed")
            return
        for msg in frames:
            try:
                entry = self.conversation.append(msg)
            except Exception:
                logger.exception(
                    "textual chat: restore append failed for kind=%r", msg.kind
                )
                continue
            # Resolved, never RUNNING — mirror _track_tool_state's terminal
            # transition for a settled tool result (SUCCESS unless the summary
            # marks a failure); non-tool rows keep DEFAULT.
            meta = msg.meta or {}
            if msg.kind == "tool_call_completed":
                summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
                entry.set_state(
                    EntryState.ERROR if summary.startswith("✗") else EntryState.SUCCESS
                )
            elif msg.kind == "tool_call_failed":
                entry.set_state(EntryState.ERROR)

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
        # Rebuild the pane from a fresh snapshot right before it becomes visible,
        # so an opened Model/Agent/Cost/Ctx pane always reflects the CURRENT state
        # (a snapshot read once at compose time would be stale by first open).
        self._refresh_pane(tab_id)
        drawer.display = True
        child = drawer.query_one(f"#{tab_id}")
        if isinstance(child, OptionList):
            child.focus()

    def _refresh_pane(self, tab_id: str) -> None:
        """Re-derive ``tab_id``'s pane content from the current canonical sources
        and update the mounted widget in place (``OptionList`` options or the
        ``Static`` text). One snapshot read feeds BOTH the rows and the parallel
        selection ids, so an ``OptionSelected`` maps back to the right id."""
        snap = self._snapshot()
        rows = self._pane_rows(tab_id, snap)
        self._pane_selection_ids[tab_id] = self._selection_ids(tab_id, snap)
        child = self.query_one(f"#{tab_id}")
        if isinstance(child, OptionList):
            child.clear_options()
            if rows:
                child.add_options(rows)
        elif isinstance(child, Static):
            child.update("\n".join(rows))

    def on_menu_bar_selected(self, event: "MenuBar.Selected") -> None:
        self._open_drawer(None if event.tab_id == "__close__" else event.tab_id)

    async def on_option_list_option_selected(
        self, event: "OptionList.OptionSelected"
    ) -> None:
        """Apply a picked Model/Agent by routing the equivalent slash command
        through the transport — the SAME ``/model <class>`` / ``/attach <name>``
        slash-command contract the plain path dispatches. Non-actionable panes
        (History/Menu = readout/Phase-5) just collapse. Then close the drawer and
        return focus to the composer."""
        tab_id = event.option_list.id
        ids = self._pane_selection_ids.get(tab_id or "", [])
        if 0 <= event.option_index < len(ids):
            chosen = ids[event.option_index]
            if tab_id == "model":
                await self._submit(f"/model {chosen}")
            elif tab_id == "agent":
                await self._submit(f"/attach {chosen}")
        self._open_drawer(None)

    def action_close_drawer(self) -> None:
        self._open_drawer(None)

    async def on_flow_view_clicked(self, event: "FlowView.Clicked") -> None:
        """Resolve a pending choice-intervention when an option chip is clicked.

        A closed-set intervention (permission confirm / choice ``ask_user`` —
        anything carrying ``meta["choices"]``) is surfaced by the presenter as
        in-flow amber chips on the row below its prompt. A click that lands on a
        chip delivers that choice's id through the transport's
        ``answer_intervention_choice`` seam — the SAME funnel
        (``InterventionHandler.deliver_answer_to``) every answer path (TUI
        free-text, A2A peer, AG-UI HITL) shares — then the entry re-presents to
        its green ``✓ resolved`` state (``EntryState.SUCCESS`` also greens the
        gutter). This handler is the ONLY choice-answer path in the Textual TTY:
        the free-text ``_submit`` path answers only no-choices interventions, so
        without it a choice-intervention (a permission prompt) is UNANSWERABLE
        here — this restores that permission-band reachability (F1)."""
        entry = event.entry
        msg = entry.item
        meta = msg.meta or {}
        choices = meta.get("choices")
        if msg.kind != "intervention" or not choices or meta.get("_chosen_label"):
            return
        # Reconstruct the presenter's body width (content − gutter) so the chip
        # geometry hit-tested here matches what was drawn.
        body_width = max(
            1, event.flow_view.scrollable_content_region.width - _GUTTER_WIDTH
        )
        if event.y != self._presenter.choice_chip_row(msg, body_width):
            return  # click was on the prompt head / hint, not the chip row
        for start, end, choice_id in choice_chip_spans(choices):
            if start <= event.x < end:
                await self._transport.answer_intervention_choice(choice_id)
                label = next(
                    (
                        c.get("label")
                        for c in choices
                        if str(c.get("id", "")) == choice_id
                    ),
                    choice_id,
                )
                entry.set_item(replace(msg, meta={**meta, "_chosen_label": label}))
                entry.set_state(EntryState.SUCCESS)
                break

    def _track_tool_state(self, msg: "OutboxMessage", entry: "Entry[OutboxMessage]") -> None:
        """Drive the Phase-2 lifecycle state of tool-call / error rows.

        A ``tool_call_started`` row (with an ``op_id`` correlation key) becomes
        RUNNING (its gutter blinks amber off the native animation clock); the
        matching ``tool_call_completed`` / ``tool_call_failed`` frame transitions
        that SAME entry to SUCCESS/ERROR (its gutter goes amber → green/coral).
        ``error`` rows go straight to ERROR. Frames without an ``op_id`` carry no
        state (DEFAULT) — they append exactly as in Phase 1, so the plain-fallback
        turn sequence is unchanged.

        ``_running_tools`` keys the RUNNING entry by ``op_id`` purely for this
        RUNNING → SUCCESS/ERROR correlation (NOT for the blink — the blink is now
        time-based in :class:`ReynGutter`, driven by the native animation tick)."""
        kind = msg.kind
        meta = msg.meta or {}
        op_id = meta.get("op_id")
        if kind == "tool_call_started":
            if op_id is not None:
                entry.set_state(EntryState.RUNNING)
                self._running_tools[op_id] = entry
        elif kind == "tool_call_completed":
            summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
            started = self._running_tools.pop(op_id, None) if op_id is not None else None
            if started is not None:
                started.set_state(
                    EntryState.ERROR if summary.startswith("✗") else EntryState.SUCCESS
                )
        elif kind == "tool_call_failed":
            started = self._running_tools.pop(op_id, None) if op_id is not None else None
            if started is not None:
                started.set_state(EntryState.ERROR)
            entry.set_state(EntryState.ERROR)
        elif kind == "error":
            entry.set_state(EntryState.ERROR)

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
                # F5b: refresh the always-visible status-values line (cost + ctx%)
                # as each turn lands, so the running cost is legible in the Textual
                # TTY like the plain path's cost_summary. Bounded by message rate
                # (far less frequent than a render loop) and guarded so a snapshot
                # read failure never kills the pump.
                try:
                    self._refresh_status()
                except Exception:
                    logger.exception("textual chat: status refresh failed")
        finally:
            self.exit()

    def _refresh_status(self) -> None:
        """Re-render the bottom status-values line from a fresh snapshot."""
        try:
            line = self.query_one(StatusLine)
        except Exception:
            return  # not yet mounted
        line.update(self._status_text())

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
            # A ``/``-prefixed line is never an intervention answer — it is a slash
            # command (dispatched by the session turn loop), so it must take the
            # normal submit path even while a free-text intervention is pending
            # (mirrors ``stream_client.submit_or_answer``'s guard). Without this,
            # a picker-issued ``/model`` / ``/attach`` (or a typed slash) during a
            # pending intervention would be mis-delivered as the answer text.
            if (
                head is not None
                and not getattr(head, "choices", None)
                and not text.startswith("/")
            ):
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
    inline: bool = False,
) -> None:
    """Run the TTY conversation-pane app until the user quits or the stream ends.

    ``inline`` selects the Textual driver: ``False`` (DEFAULT) runs full-screen
    (alt-screen), ``True`` runs the legacy bounded inline driver. The caller
    (:func:`~reyn.interfaces.repl.client_driver.run_chat_client`) resolves this
    from ``chat.render_mode`` (#3273) — see :func:`resolve_render_mode`.

    Full-screen is the default because two inline-driver bugs made bounded inline
    unshippable: on resize the old bounded frame is not cleared so stale copies
    stack (#3285), and the conversation pane collapses to ~1 line regardless of
    terminal height (#3286). Both are owned by Textual's inline driver, so reyn
    cannot fix them in inline mode; alt-screen sidesteps the driver entirely and
    both vanish. The scrollback-preservation rationale that originally motivated
    inline is now redundant — alt-screen auto-saves/restores terminal scrollback
    on enter/exit, and Phase 5 restore rebuilds the conversation from
    ``history.jsonl`` on restart. ``inline=True`` remains selectable as an escape
    hatch (``chat.render_mode: inline``) for scrollback-preferring users, with
    the #3285/#3286 caveat. Returns so the driver's caller can tear the transport
    down + print the cost summary.
    """
    app = TextualChatApp(
        transport=transport,
        read_model=read_model,
        agent_name=agent_name,
        config=config,
    )
    await app.run_async(inline=inline)
