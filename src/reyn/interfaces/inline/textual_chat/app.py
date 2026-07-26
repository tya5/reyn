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
app-side blink timer. A RUNNING tool row additionally grows a live spinner +
elapsed BODY, driven by a viewport-gated per-entry ``FlowView.animate_entry``
that is stopped when the tool completes (Phase ②, #3283).

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` / ``textual_flowview``
imports never reach an always-loaded module.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Callable

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
from reyn.interfaces.transport.agui.state import RemoteQueueView
from reyn.interfaces.transport.frames import FrameTag

from ._meta_keys import ORPHANED_RESULT_KIND as _ORPHANED_RESULT_KIND
from .chrome import (
    _MENU_TABS,
    Composer,
    MenuBar,
    StatusLine,
    _history_option_content,
    build_drawer_pane,
    pane_payload,
    status_line_text,
)
from .gutter import _RUNNING_FRAME_PERIOD, ReynGutter
from .intervention_panel import InterventionPanel
from .presenter import (
    _RESULT_KIND_KEY,
    _RESULT_META_KEY,
    _RUNNING_SINCE_KEY,
    ReynPresenter,
    _neutralized_label,
)
from .restore import project_restored_frames
from .sent_queue import SentQueue

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

# Turn-end event types (#72): when one of these lands on the EVENT-tag frame
# path, any tool row still RUNNING is a confirmed ORPHAN — its completion frame
# can never arrive for THIS turn, since the turn itself just ended. Mirrors the
# plain renderer's ``on_chat_event`` turn-end branch
# (``src/reyn/interfaces/repl/renderer.py``): ``turn_settled`` fires for EVERY
# turn kind (incl. slash short-circuits) and is the primary signal;
# ``turn_completed`` / ``turn_cancelled`` are belt-and-suspenders. Deliberately
# NOT a max-age timer — a time threshold cannot distinguish an orphan (tool
# truly gone) from a slow-but-alive tool (a legitimately long ``exec``); the
# turn-boundary signal is deterministic instead of guessed.
_TURN_END_EVENT_TYPES = frozenset({"turn_settled", "turn_completed", "turn_cancelled"})

#: FlowView gutter column width (state-coloured marker). Wired into
#: ``compose``'s ``FlowView(gutter_width=…)`` config.
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

    Phase ② (#3283) adds a LIVE BODY to a RUNNING tool row: while in flight the
    row shows a spinner + an app-computed ``elapsed Ns`` under its ``tool(args)``
    header, driven by a per-entry ``FlowView.animate_entry`` (viewport-gated: an
    off-screen RUNNING tool neither spins nor recomputes). On completion the row
    SETTLES IN PLACE — the per-entry animation is stopped and the result is
    COALESCED into the SAME entry as a ``⎿ <result>`` sub-line (CC's
    ``⏺ tool(args)`` + ``⎿ result`` block; the started + completed frames are ONE
    row, not two). A completion with no matching started entry still appends its
    own row, so nothing regresses. This composes with ①: ① animates the GUTTER
    glyph off the always-on ``animation_fps`` clock, ② the tool BODY off a
    per-entry timer. Tool frames carry no elapsed/progress (ADR finding D2), so
    elapsed is computed from the app-side arrival time (``self._clock``); the
    indicator is a spinner (indeterminate), NOT a progress bar.

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

    #3299 P1 moved intervention interaction OUT of the FlowView into a grouped
    :class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
    widget between the flow and the input row (atomic display-swap +
    input-swap + chip-retire — see :meth:`_present_intervention`'s docstring).
    P2 gave the panel FIFO re-route (resolving the displayed intervention
    swapped the next queued one into the SAME form); #3308 (P5) replaces that
    re-route with TABS — one :class:`~textual.widgets.TabPane` per PENDING
    intervention, added the moment its frame arrives
    (:meth:`_present_intervention` → ``InterventionPanel.add_pending``) and
    never swapped out from under the user. A ``kind="intervention"`` frame
    arriving on the pump (:meth:`_ingest_frame`) appends a THIN pending flow
    placeholder and adds a new tab to the panel — a
    :class:`~textual.widgets.RadioSet` for a closed-set intervention
    (``meta["choices"]``), a plain :class:`~textual.widgets.Input` otherwise.
    The panel's OWN ``TabbedContent`` semantics give the "only steal focus
    while idle" invariant for free: a new tab auto-activates only when the
    panel was hidden (nothing pending before this arrival); while another
    intervention is already showing, the new tab is added WITHOUT moving the
    active selection (never stealing it from a tab the user is already
    looking at — the F1-class accident class this PR closes structurally,
    see the panel module's docstring). Pre-highlighting the RadioSet's FIRST
    option (#3299 P2 owner decision (A): a blind ``Enter`` answers it) is now
    UNCONDITIONAL on every tab activation — first show AND every explicit
    tab switch alike (#3308 §5) — since the P2 re-route accident this
    conditional once guarded against no longer exists (the active tab never
    moves except by explicit user navigation). Selecting/submitting in a tab
    (:meth:`on_intervention_panel_choice_selected` /
    :meth:`on_intervention_panel_text_submitted`) delivers the answer through
    the UNCHANGED transport funnel (``answer_intervention_choice`` /
    ``answer_intervention_text``) targeted at THAT tab's intervention id
    (#3299 P2, R1 by-id delivery — the message itself carries which pending
    entry it came from, so there is no ambiguous "currently displayed" state
    to track at the app level anymore), resolves that SAME flow entry in
    place to a "✓ answered" record (:meth:`_resolve_intervention`) and marks
    the tab ✓-answered + inert — but the tab STAYS (never removed) until
    EVERY pending intervention has resolved, at which point the whole panel
    collapses (:meth:`_resolve_intervention`). ``Esc``/``Tab`` inside the
    panel (:meth:`on_intervention_panel_dismissed`) return focus to the
    Composer WITHOUT answering — no intervention's state changes (the #3300
    sent-queue durably holds any new Composer submit while any stay pending;
    no black-hole guard is needed here, see the PR body). The Composer itself
    is now EXCLUSIVELY for new turns — it no longer reads
    ``pending_intervention_head()`` at all.
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

    #: Per-entry BODY animation rate (Hz) for the live RUNNING-tool indicator
    #: (Phase ②). Drives ``FlowView.animate_entry`` so the spinner + elapsed body
    #: of an in-flight tool row re-presents at this cadence — but ONLY while the
    #: entry is on screen (``animate_entry`` is viewport-gated: off-screen RUNNING
    #: tools neither spin nor recompute). Matches the plain renderer's ~12fps
    #: working spinner. Distinct from :data:`ANIMATION_FPS` (the always-on GUTTER
    #: blink clock, Phase ①): ① animates the gutter glyph, ② the tool body.
    RUNNING_BODY_FPS = 12.0

    def __init__(
        self,
        *,
        transport: "ClientTransport",
        read_model: "ChatReadModel | None" = None,
        agent_name: str = "default",
        config=None,
        clock: "Callable[[], float]" = time.monotonic,
        presenter: "ReynPresenter | None" = None,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._read_model = read_model
        self._agent_name = agent_name
        self._config = config
        # App-side monotonic clock for the RUNNING-tool elapsed timer: tool frames
        # carry no RUNNING-start timestamp (ADR finding D2), so elapsed is computed
        # from when the started frame arrived here. Injectable so a test drives the
        # live indicator deterministically; the presenter reads the SAME clock.
        self._clock = clock
        self.conversation: "FlowModel[OutboxMessage]" = FlowModel()
        # One presenter instance the FlowView DRAWS entries with. Injectable
        # (default a fresh one on the app clock) so a test can observe
        # presentation.
        self._presenter = presenter or ReynPresenter(clock=self._clock)
        # Running tool-call entries keyed by op_id (== the dispatcher's
        # deterministic args_hash, meta["op_id"]) so a later completion/failure
        # frame transitions the SAME entry RUNNING → SUCCESS/ERROR (CC parity).
        self._running_tools: "dict[object, Entry[OutboxMessage]]" = {}
        # ALL pending interventions' flow entries (#3299 P2 — was single-slot
        # in P1, overwritten by a second concurrent pending intervention, an
        # architect self-review finding: ``outstanding_interventions`` is a
        # multi-entry structure by design, e.g. restore's FIFO re-enqueue),
        # keyed by intervention id (falling back to a synthetic per-entry key
        # when a frame carries no id, so tracking never breaks — delivery then
        # falls back to head-targeted for that entry, matching pre-P2
        # behavior). Each value is ``(entry, intervention_id | None)`` — the
        # flow entry :meth:`_resolve_intervention` updates IN PLACE, and the
        # real id to target at the transport (``None`` disables by-id
        # targeting for that entry). #3308 (P5) drops the THIRD P2 tuple slot
        # (the original frame, kept only to re-populate a re-routed panel) —
        # tabs never re-populate, each pending intervention keeps its own
        # tab for its whole pending lifetime, so there is nothing to replay.
        self._pending_ivs: "dict[object, tuple[Entry[OutboxMessage], str | None]]" = {}
        # #3300 P2b: the client-side sent-queue model — the SAME seq-gated
        # merge P2a built (``RemoteQueueView``, reused as-is, not
        # reinvented) driving BOTH the local and remote transport, since the
        # read-model now projects queue/turn_active/queue_seq uniformly for
        # each (``ChatReadModel.snapshot()``, see ``read_model.py``). Seeded
        # once from a fresh snapshot on the FIRST frame the pump processes
        # (:meth:`_seed_queue_view`) — by then a remote connection's
        # connect-time STATE_SNAPSHOT has already been applied to the
        # transport (emitter.py's "Reconnect snapshots first (A4)"), so the
        # baseline is late-joiner-correct; a local read is always live so an
        # early seed is harmless there.
        self._queue_view = RemoteQueueView()
        self._queue_seeded = False
        # A queued item's ``meta`` (ADR-0039 attribution) — ``apply_user_submitted``
        # deliberately stores only msg_id/chain_id/text on
        # ``RemoteQueueView.items`` (P2a's delta contract, reused unmodified,
        # not reinvented); this side table is what carries a promoted item's
        # meta into its flow entry, keyed by msg_id and popped on promotion.
        # Populated from TWO sources: the live ``user_submitted`` delta
        # (:meth:`_handle_user_submitted_event`) and, for an item already
        # queued at connect time, the snapshot seed (:meth:`_seed_queue_view`
        # — ``queued_user_messages()`` now projects ``meta`` too, #3300 P2b
        # co-vet fix, so a late-joiner's promoted item is attributed exactly
        # like a delta-path one).
        self._queue_item_meta: "dict[str, dict]" = {}
        # #3300 Y-client: msg_id -> cancelled text, for a cancel THIS client
        # itself issued (:meth:`on_sent_queue_cancelled`), populated BEFORE
        # the ``cancel_queued`` call. The composer restore
        # (:meth:`_restore_cancelled_text`) is driven ONLY off the matching
        # ``inbox_cancel`` delta actually arriving
        # (:meth:`_handle_inbox_cancel_event`) — never off this call's return
        # value — so it never fires for a cancel that raced an already-
        # dispatched item (no delta ever follows a no-op) and stays
        # consistent with the row-removal contract (also delta-driven, never
        # return-value-driven). Canceller-local by construction: only the
        # client that populated this entry ever restores anything; every
        # other client applies the SAME delta as a plain removal.
        self._pending_own_cancels: "dict[str, str]" = {}
        # Per-picker parallel id lists (class names / agent names), keyed by tab
        # id and kept in lock-step with the OptionList options a pane was last
        # refreshed with, so an ``OptionSelected.option_index`` maps back to the
        # canonical id the ``/model`` / ``/attach`` slash needs. Populated on each
        # drawer refresh (:meth:`_refresh_pane`) from the SAME snapshot that built
        # the rows, so the option row and its id never drift.
        self._pane_selection_ids: "dict[str, list[str]]" = {}
        # #3288 ③c: in-flight streamed reply, keyed by ``chain_id`` — the SAME
        # authoritative correlation id ``RouterLoop._emit_agent_delta`` stamps
        # on every ``agent_delta`` chat-event AND the one the terminal
        # ``kind="agent"`` OutboxMessage carries in its ``meta["chain_id"]``
        # (never a guessed key — text-match correlation was tried and
        # reverted earlier in this arc, issue #3288/#3309). Each value is
        # ``(entry, accumulated_text)``: the FIRST delta for a chain_id
        # appends ONE new flow entry; every SUBSEQUENT delta for that SAME
        # chain_id updates that SAME entry in place
        # (:meth:`_handle_agent_delta_event`) rather than appending a second
        # row. The terminal completion (:meth:`_ingest_frame`'s ``kind ==
        # "agent"`` branch) pops this entry and finalizes it with the
        # authoritative full text — the ONLY place a streamed reply's entry
        # is removed from this map, so a chain_id can never leak past its
        # turn's completion.
        self._streaming_replies: "dict[str, tuple[Entry[OutboxMessage], str]]" = {}

    def compose(self) -> ComposeResult:
        # Held so the frame pump can start/stop the per-entry BODY animation
        # (``animate_entry``/``stop_entry_animation``) that drives a RUNNING tool
        # row's live spinner + elapsed (Phase ②).
        self._flow: "FlowView[OutboxMessage]" = FlowView(
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
        yield self._flow
        # #3299 P1: the grouped intervention panel sits BETWEEN the flow and
        # the input row (region order shared with the sibling #3300 queue arc:
        # conversation / intervention panel / sent-queue / input).
        # Collapsed by default (``display=False`` — see
        # ``InterventionPanel.on_mount``); shown + auto-focused only while an
        # intervention is pending (:meth:`_present_intervention`).
        self._iv_panel = InterventionPanel(id="intervention-panel")
        yield self._iv_panel
        # #3300 P2b: the sent-queue region sits BETWEEN the intervention
        # panel and the input row (region order: conversation / intervention
        # panel / sent-queue / input — pinned by the architect design pass so
        # the sibling #3299/#3300 P1 coders never collide on this zone).
        # Collapsed by default (``display=False`` — see ``SentQueue.on_mount``);
        # shown while at least one message is queued, undispatched.
        self._sent_queue = SentQueue(id="sent-queue")
        yield self._sent_queue
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
        is hydrated).

        **Security**: this is LLM-/user-derived conversation content reaching
        the History drawer's ``OptionList`` — the SAME injection class as the
        #3302 panel-label bug, just a different widget. Neutralized (ESC/
        control strip) HERE, at the source, before the row string is even
        assembled — the fidelity half (never a bare ``str`` handed to
        ``OptionList``) is a SEPARATE guard at the two widget-construction
        call sites (:func:`~reyn.interfaces.inline.textual_chat.chrome.
        build_drawer_pane` / :meth:`_refresh_pane`), not here."""
        rows: list[str] = []
        for entry in self.conversation:
            msg = entry.item
            if msg.kind not in ("user", "reply", "agent"):
                continue
            body = _neutralized_label(msg.text or "").strip()
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
        Every restored entry is RESOLVED, never RUNNING: a restored tool turn is
        already projected into the SAME coalesced ``tool_call_started`` shape
        the live path's :meth:`_coalesce_tool_result` settles a completed tool
        into (call header + folded result, one entry — see
        ``restore.project_restored_frames``'s docstring), so this method just
        derives the terminal SUCCESS/ERROR state from the coalesced result
        (the ``if msg.kind == "tool_call_completed"`` branch a pre-coalesce
        restore shape would have hit no longer fires for tool rows; user/agent
        rows keep DEFAULT, their live state). A REMOTE read model returns an
        empty log (frame-sufficiency: past turns are not on the wire) → this is
        a no-op and the pane starts blank, exactly as before. Fully guarded — a
        restore failure must never stop the app from mounting and pumping live
        frames."""
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
            # Resolved, never RUNNING — mirror the completion handler's terminal
            # transition for a settled tool result (SUCCESS unless the summary
            # marks a failure); non-tool rows keep DEFAULT.
            meta = msg.meta or {}
            if msg.kind == "tool_call_started" and _RESULT_KIND_KEY in meta:
                result_meta = meta.get(_RESULT_META_KEY) or {}
                if meta[_RESULT_KIND_KEY] == "tool_call_failed":
                    entry.set_state(EntryState.ERROR)
                else:
                    summary = summarize_tool_result(
                        meta.get("tool"), result_meta.get("result")
                    )
                    entry.set_state(
                        EntryState.ERROR
                        if summary.startswith("✗")
                        else EntryState.SUCCESS
                    )
            elif msg.kind == "tool_call_completed":
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
        selection ids, so an ``OptionSelected`` maps back to the right id.

        The History tab's rows get the SAME ``Content``-literal fidelity wrap
        :func:`~reyn.interfaces.inline.textual_chat.chrome.build_drawer_pane`
        applies at initial ``compose`` time (:func:`~reyn.interfaces.inline.
        textual_chat.chrome._history_option_content`) — this refresh path is a
        SEPARATE call site from that initial build (``OptionList.add_options``
        vs the constructor), so it needs its own, independently-verified wrap;
        the row TEXT itself is already neutralized upstream, in
        :meth:`_history_turns`."""
        snap = self._snapshot()
        rows = self._pane_rows(tab_id, snap)
        self._pane_selection_ids[tab_id] = self._selection_ids(tab_id, snap)
        child = self.query_one(f"#{tab_id}")
        if isinstance(child, OptionList):
            child.clear_options()
            if rows:
                options = _history_option_content(rows) if tab_id == "history" else rows
                child.add_options(options)
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

    def _present_intervention(
        self, msg: "OutboxMessage", entry: "Entry[OutboxMessage]"
    ) -> None:
        """Route a newly-arrived intervention frame to the panel (#3299 P1/P2,
        tab-ified #3308 P5).

        The flow ``entry`` stays a THIN pending placeholder (the presenter
        renders prompt + a dim "respond below" hint, never chips — see
        :meth:`~reyn.interfaces.inline.textual_chat.presenter.ReynPresenter._present_intervention_pending`);
        the interactive form (closed-set select / free-text input) lives
        entirely in its OWN tab inside :attr:`_iv_panel`.

        Multi-pending (#3299 P2, tab-ified #3308): ``outstanding_interventions``
        can hold SEVERAL pending entries at once (e.g. restore's FIFO
        re-enqueue), so every arriving intervention is tracked in
        :attr:`_pending_ivs` (keyed by its id) AND gets its own tab
        (``InterventionPanel.add_pending``) regardless of display order. The
        panel's own ``TabbedContent`` auto-activates a new tab ONLY when it
        was previously empty (verified against the installed Textual 8.2.8 —
        see the panel module's docstring), so an arrival while another
        intervention is already showing never steals the active tab. This is
        still an ATOMIC swap with the retired in-flow chips: display and
        input (:meth:`on_intervention_panel_choice_selected` /
        :meth:`on_intervention_panel_text_submitted`) moved together, so there
        is never a moment where both the panel AND a chip/composer-match path
        are live for the same intervention."""
        meta = msg.meta or {}
        iv_id = meta.get("intervention_id")
        key: object = iv_id if iv_id else id(entry)
        self._pending_ivs[key] = (entry, iv_id)
        prompt = str(meta.get("prompt") or msg.text or "")
        detail = meta.get("detail")
        choices = meta.get("choices")
        self._iv_panel.add_pending(key, prompt=prompt, detail=detail, choices=choices)

    def _resolve_intervention(self, key: object, answer_label: str) -> None:
        """Settle intervention ``key``'s flow entry + tab once an answer has
        been delivered through the transport funnel (#3308 — replaces P2's
        single-form FIFO re-route: the tab STAYS, ✓-marked and inert, rather
        than being swapped out for the next pending one).

        The SAME entry is updated in place (churn-zero, #3299 P2 §4) to a
        ``✓ answered: <label>`` record
        (:meth:`ReynPresenter._present_intervention_pending` reads the
        ``_answer_label`` meta key). The entry's :class:`EntryState` goes to
        ``DEFAULT`` — not ``SUCCESS``/``ERROR`` (an intervention answer is
        neither an outcome to celebrate nor a failure, the #3296
        don't-fabricate-a-classification lesson) and not ``RUNNING`` (would
        trip the #72 orphan-sweep + the ② live-spinner, and an intervention is
        not a tool). Only once EVERY pending intervention has resolved does
        the panel collapse and focus return to the Composer — the resolved
        leg of the focus lifecycle (pending → panel auto-focus, Esc/Tab →
        Composer, all-resolved → Composer)."""
        self._iv_panel.mark_answered(key, answer_label)
        resolved = self._pending_ivs.pop(key, None)
        if resolved is not None:
            entry, _iv_id = resolved
            meta = entry.item.meta or {}
            entry.set_item(replace(entry.item, meta={**meta, "_answer_label": answer_label}))
            entry.set_state(EntryState.DEFAULT)
        if self._pending_ivs:
            # Other tabs are still pending — the panel (and whichever tab was
            # active) stays exactly as it was; no re-route (#3308).
            return
        self._iv_panel.collapse_all()
        self.query_one(Composer).focus()

    async def on_intervention_panel_choice_selected(
        self, event: "InterventionPanel.ChoiceSelected"
    ) -> None:
        """A closed-set option was picked in one tab: deliver its id through
        the UNCHANGED transport funnel (``answer_intervention_choice`` —
        ``InterventionHandler.deliver_answer_to`` under the hood, the SAME
        funnel every answer path — TUI, A2A peer, AG-UI HITL — shares),
        targeted at THAT tab's intervention id (#3299 P2, R1 — the event
        itself carries ``event.key``, identifying exactly which pending
        intervention this is; #3308 removes the need for an app-level
        "currently displayed" key entirely). This is the F1 permission-band
        reachability witness, restored through the panel instead of a chip
        click."""
        _entry, iv_id = self._pending_ivs.get(event.key, (None, None))
        await self._transport.answer_intervention_choice(
            event.choice_id, intervention_id=iv_id
        )
        self._resolve_intervention(event.key, event.label)

    async def on_intervention_panel_text_submitted(
        self, event: "InterventionPanel.TextSubmitted"
    ) -> None:
        """A free-text answer was submitted in one tab's Input: deliver it
        through the UNCHANGED ``answer_intervention_text`` transport funnel,
        targeted at THAT tab's intervention id (#3299 P2, R1; #3308 by
        ``event.key``, same as the choice path)."""
        _entry, iv_id = self._pending_ivs.get(event.key, (None, None))
        await self._transport.answer_intervention_text(event.text, intervention_id=iv_id)
        self._resolve_intervention(event.key, event.text)

    def on_intervention_panel_dismissed(
        self, event: "InterventionPanel.Dismissed"
    ) -> None:
        """Esc/Tab inside the panel: return focus to the Composer WITHOUT
        answering — the escape hatch of the focus lifecycle. Every pending
        intervention stays exactly as it was (the panel stays open); a new
        Composer submit durably queues on the inbox rather than black-holing
        (#3300's sent-queue — see the PR body for why #3299 needs no guard of
        its own here)."""
        self.query_one(Composer).focus()

    def _ingest_frame(self, msg: "OutboxMessage") -> None:
        """Fold one display frame into the retained model — appending a new entry,
        or COALESCING a correlated tool result into its RUNNING started entry.

        A ``tool_call_completed`` / ``tool_call_failed`` frame whose ``op_id``
        matches a tracked RUNNING tool does NOT append a second row: it SETTLES the
        started entry in place (:meth:`_coalesce_tool_result` — stop the ② live
        spinner, fold the ``⎿ result`` into the same entry, go SUCCESS/ERROR), so a
        call and its result read as ONE block (CC's ``⏺ tool(args)`` + ``⎿ result``,
        the PoC's ``_present_tool_call`` grouping). A ``kind="agent"`` completion
        whose ``meta["chain_id"]`` matches an in-flight streamed reply
        (:attr:`_streaming_replies`, #3288 ③c) does not append a second entry
        either: it FINALIZES the same entry the deltas coalesced into, with the
        completion's authoritative full text (L9 whole-persist's source of
        truth), and pops the tracked chain_id. Every OTHER frame — including a
        completion with NO matching started entry (already settled / uncorrelated)
        — is appended as its own entry: an ``intervention`` frame routes to
        :meth:`_present_intervention` (the panel, #3299 P1), everything else to
        :meth:`_apply_lifecycle_state`, so nothing regresses for the
        plain-fallback turn sequence."""
        kind = msg.kind
        meta = msg.meta or {}
        op_id = meta.get("op_id")
        if kind in ("tool_call_completed", "tool_call_failed") and op_id is not None:
            started = self._running_tools.pop(op_id, None)
            if started is not None:
                self._coalesce_tool_result(started, msg)
                return
        if kind == "agent":
            chain_id = meta.get("chain_id")
            streaming = self._streaming_replies.pop(chain_id, None) if chain_id else None
            if streaming is not None:
                entry, _partial_text = streaming
                entry.set_item(replace(entry.item, text=msg.text, meta=meta))
                return
        entry = self.conversation.append(msg)
        if kind == "intervention":
            self._present_intervention(msg, entry)
        else:
            self._apply_lifecycle_state(msg, entry)

    def _apply_lifecycle_state(
        self, msg: "OutboxMessage", entry: "Entry[OutboxMessage]"
    ) -> None:
        """Drive the Phase-2 lifecycle state + Phase-② live body of a NEWLY appended
        row (the non-coalesced path).

        A ``tool_call_started`` row (with an ``op_id`` correlation key) becomes
        RUNNING (its gutter blinks amber off the native animation clock) AND grows
        a LIVE body — a spinner + app-computed ``elapsed Ns`` — driven by
        :meth:`_begin_running_indicator`; its matching completion later coalesces
        into it (:meth:`_ingest_frame`). An UNCORRELATED ``tool_call_failed`` (no
        tracked started) or an ``error`` row goes straight to ERROR (coral gutter +
        tint). Frames without an ``op_id`` carry no state (DEFAULT) — they append
        exactly as in Phase 1, so the plain-fallback turn sequence is unchanged.

        ``_running_tools`` keys the RUNNING entry by ``op_id`` for the settle: it is
        the handle the completion frame coalesces + stops the animation on. The
        gutter blink itself is time-based in :class:`ReynGutter`, driven by the
        native animation tick."""
        kind = msg.kind
        op_id = (msg.meta or {}).get("op_id")
        if kind == "tool_call_started" and op_id is not None:
            entry.set_state(EntryState.RUNNING)
            self._running_tools[op_id] = entry
            self._begin_running_indicator(entry)
        elif kind in ("tool_call_failed", "error"):
            entry.set_state(EntryState.ERROR)

    def _coalesce_tool_result(
        self, started: "Entry[OutboxMessage]", result_msg: "OutboxMessage"
    ) -> None:
        """Settle a RUNNING tool row IN PLACE with its result (② completion).

        Stops the per-entry ② live-spinner animation (``stop_entry_animation`` — no
        leaked timer survives completion), then folds the completion frame's kind +
        meta into the started entry's item (stripping the :data:`_RUNNING_SINCE_KEY`
        live marker and stashing the result under :data:`_RESULT_KIND_KEY` /
        :data:`_RESULT_META_KEY`) so the presenter renders ``tool(args)`` + a
        ``⎿ <result>`` sub-line in this SAME entry — no separate result row, no
        lingering spinner. Finally sets the terminal state: ERROR (coral) on a
        failure or a ``✗`` result summary, SUCCESS (green) otherwise. Each step is
        guarded so a settle failure never kills the pump."""
        try:
            self._flow.stop_entry_animation(started)
        except Exception:
            logger.exception("textual chat: could not stop running-tool animation")
        started_meta = started.item.meta or {}
        result_meta = result_msg.meta or {}
        merged = {k: v for k, v in started_meta.items() if k != _RUNNING_SINCE_KEY}
        merged[_RESULT_KIND_KEY] = result_msg.kind
        merged[_RESULT_META_KEY] = result_meta
        try:
            started.set_item(replace(started.item, meta=merged))
        except Exception:
            logger.exception("textual chat: could not coalesce tool result")
        if result_msg.kind == "tool_call_failed":
            started.set_state(EntryState.ERROR)
        else:
            summary = summarize_tool_result(
                started_meta.get("tool"), result_meta.get("result")
            )
            started.set_state(
                EntryState.ERROR if summary.startswith("✗") else EntryState.SUCCESS
            )

    def _sweep_orphaned_running_tools(self) -> None:
        """Force-settle any tool row still RUNNING at a TURN BOUNDARY (#72).

        An ORPHAN is a tool whose completion frame never arrives — its report is
        lost, or the turn ends without it — leaving its ② live spinner
        (``⠙ elapsed Ns``) spinning FOREVER. The fix is deterministic rather than
        a max-age timer: a time threshold cannot tell an orphan (tool truly gone)
        apart from a slow-but-alive tool (a legitimately long ``exec``), but when
        the TURN itself settles, there can be no more completions for that
        turn's tools — any entry still in :attr:`_running_tools` at that instant
        is a confirmed orphan. Called from :meth:`_pump_frames` on
        ``turn_settled`` / ``turn_completed`` / ``turn_cancelled``.

        Each orphan is settled exactly like :meth:`_coalesce_tool_result` (stop
        the ② animation, strip :data:`_RUNNING_SINCE_KEY`, stash a result-kind
        marker so the presenter folds a ``⎿`` sub-line under the header) EXCEPT
        the stashed kind is the NEUTRAL sentinel
        :data:`~reyn.interfaces.inline.textual_chat._meta_keys.ORPHANED_RESULT_KIND`
        — never a failure. The tool did not fail; its report simply never
        arrived, so the row goes :attr:`EntryState.CANCELLED` (dim gutter, same
        as ``ReynGutter``'s DEFAULT/CANCELLED colour) rather than ``SUCCESS``
        (would imply it worked) or ``ERROR`` (would imply it failed — the #3296
        don't-fabricate-a-failure lesson). Every step is guarded so one orphan's
        settle failure never kills the pump or leaves the others un-swept; the
        dict is cleared unconditionally at the end so no turn's leftovers bleed
        into the next."""
        for entry in list(self._running_tools.values()):
            try:
                self._flow.stop_entry_animation(entry)
            except Exception:
                logger.exception(
                    "textual chat: could not stop orphaned-tool animation"
                )
            try:
                meta = {
                    k: v
                    for k, v in (entry.item.meta or {}).items()
                    if k != _RUNNING_SINCE_KEY
                }
                meta[_RESULT_KIND_KEY] = _ORPHANED_RESULT_KIND
                entry.set_item(replace(entry.item, meta=meta))
            except Exception:
                logger.exception(
                    "textual chat: could not settle orphaned-tool entry"
                )
            try:
                entry.set_state(EntryState.CANCELLED)
            except Exception:
                logger.exception(
                    "textual chat: could not set orphaned-tool state"
                )
        self._running_tools.clear()

    def _begin_running_indicator(self, entry: "Entry[OutboxMessage]") -> None:
        """Start the live spinner + elapsed body for a RUNNING tool entry (②).

        Stamps the monotonic START time into the entry's item meta
        (:data:`_RUNNING_SINCE_KEY`) — its presence is what makes the presenter
        render the live indicator instead of the static ``tool(args)`` line — then
        registers a viewport-gated per-entry animation (``FlowView.animate_entry``)
        whose tick simply re-presents the body (``entry.update()``), so the spinner
        frame + elapsed count advance with wall time while the row is ON SCREEN
        (off-screen RUNNING tools are auto-paused by ``animate_entry`` — no spin,
        no recompute). The animation is released by :meth:`_coalesce_tool_result`
        on completion (and dropped automatically by flowview if the entry is
        removed), so it never outlives the RUNNING state. Fully guarded: a live
        indicator is cosmetic, so a failure to start it must never break the pump
        (the row still shows its static state-coloured gutter).

        Body re-present cadence: the per-entry ``animate_entry`` tick bumps the
        entry revision (``entry.update()``); the re-present materializes on the
        next viewport paint — immediately at up to ``RUNNING_BODY_FPS`` while the
        conversation is scrolled (the on-screen band is fresh), and at least at the
        native ``animation_fps`` gutter-tick cadence otherwise (that repaint
        re-presents the bumped revision). So the spinner + elapsed always advance;
        they are simply smoother in a scrolled conversation."""
        try:
            entry.set_item(
                replace(
                    entry.item,
                    meta={**(entry.item.meta or {}), _RUNNING_SINCE_KEY: self._clock()},
                )
            )
            self._flow.animate_entry(entry, 1.0 / self.RUNNING_BODY_FPS, lambda e: e.update())
        except Exception:
            logger.exception("textual chat: could not start running-tool indicator")

    def _seed_queue_view(self) -> None:
        """Seed :attr:`_queue_view` from a fresh read-model snapshot — called
        once, on the FIRST frame the pump processes (#3300 P2b).

        The read-model projects ``queue``/``turn_active``/``queue_seq``
        uniformly for local and remote (``read_model.py``'s
        ``project_remote_snapshot`` mirrors ``interfaces/repl/status.py``'s
        ``_snapshot()``), so ONE call seeds the seq-gate baseline correctly
        for either transport: local is always live (no wire delay), and for
        remote the connect-time ``STATE_SNAPSHOT`` has already reached the
        transport by the time frame #1 is yielded (emitter.py's "Reconnect
        snapshots first (A4)"), so this is late-joiner-correct. Any item the
        snapshot already carries (a submission from BEFORE this client
        attached) is rendered into the sent-queue region immediately."""
        snap = self._snapshot() or {}
        self._queue_view.apply_snapshot(
            queue=snap.get("queue", []),
            turn_active=snap.get("turn_active", False),
            queue_seq=snap.get("queue_seq", 0),
        )
        for item in self._queue_view.queue():
            msg_id = item.get("msg_id")
            if msg_id:
                self._sent_queue.show_item(msg_id, str(item.get("text", "")))
                # #3300 P2b co-vet fix: a snapshot-seeded item's ``meta``
                # (ADR-0039 attribution — carried through by
                # ``RemoteQueueView.apply_snapshot``'s ``dict(item)`` copy,
                # since ``queued_user_messages()`` now projects it) must land
                # in the SAME side table the delta path uses, or a promoted
                # snapshot-seeded item loses its ``[actor]`` prefix (a peer's
                # queued message misattributing as a plain operator line for
                # a late-joining client).
                self._queue_item_meta[msg_id] = dict(item.get("meta") or {})

    def _handle_user_submitted_event(self, event) -> None:
        """MATERIALIZE exit (#3300 P2b, sent-queue exit contract §6a): a
        ``user_submitted`` delta appears in the sent-queue region — NOT
        immediately as a flow entry (that was P1 C's behavior; P2b replaces
        it with this staging step). Applies the seq-gate
        (:meth:`RemoteQueueView.apply_user_submitted`) before rendering, so a
        stale/already-superseded delta is a no-op."""
        data = event.data or {}
        msg_id = data.get("msg_id")
        chain_id = data.get("chain_id")
        text = str(data.get("text", ""))
        seq = data.get("seq", 0)
        applied = self._queue_view.apply_user_submitted(
            msg_id=msg_id, chain_id=chain_id, text=text, seq=seq,
        )
        if applied and msg_id:
            self._queue_item_meta[msg_id] = dict(data.get("meta") or {})
            self._sent_queue.show_item(msg_id, text)

    def _handle_turn_started_event(self, event) -> None:
        """PROMOTE exit (#3300 P2b, sent-queue exit contract §6a): a
        ``turn_started`` delta whose ``chain_id`` matches a queued item
        removes it from the sent-queue region and appends it as a flow entry
        (the user line) — the dispatch promotion. ``turn_started`` fires for
        EVERY turn kind, not only ``user`` ones (session.py: "harmless for
        non-queue turns"), so a non-matching chain_id is correctly a no-op
        here (nothing queued for it).

        The pre-call match snapshot is load-bearing: ``apply_turn_started``
        returns ``True`` whenever the seq-gate ACCEPTS the delta, regardless
        of whether any item actually matched — checking membership only
        AFTER the call would miss the case where nothing matched (never
        promote), and re-checking on a stale/rejected delta (``False``) would
        double-promote an item this app already promoted once."""
        data = event.data or {}
        chain_id = data.get("chain_id")
        seq = data.get("seq", 0)
        matches = [
            item for item in self._queue_view.queue()
            if item.get("chain_id") == chain_id
        ]
        applied = self._queue_view.apply_turn_started(chain_id=chain_id, seq=seq)
        if not applied:
            return
        from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

        for item in matches:
            msg_id = item.get("msg_id")
            if msg_id:
                self._sent_queue.remove_item(msg_id)
            meta = self._queue_item_meta.pop(msg_id, {}) if msg_id else {}
            text = _neutralized_label(str(item.get("text", "")))
            self._ingest_frame(OutboxMessage(kind="user", text=text, meta=meta))

    def _handle_inbox_cancel_event(self, event) -> None:
        """REMOVE exit (#3300 Y-client, sent-queue exit contract §6a): an
        ``inbox_cancel`` delta removes the matching queued item from the
        sent-queue region. Applies the SAME seq-gate protocol as the
        materialize/promote deltas (``RemoteQueueView.apply_inbox_cancel``),
        so a stale/already-superseded delta is a no-op, exactly like
        :meth:`_handle_user_submitted_event`/:meth:`_handle_turn_started_event`.

        Server-authoritative: removal happens for EVERY client from this SAME
        delta, never from a client-local "cancel succeeded" return value. If
        THIS client is the one that issued the cancel (tracked in
        :attr:`_pending_own_cancels`, populated by
        :meth:`on_sent_queue_cancelled` before the ``cancel_queued`` call),
        the cancelled text is ADDITIONALLY restored into the composer
        (:meth:`_restore_cancelled_text`) — canceller-local, per the owner's
        ratified contract (issue #3300 §6a); every OTHER client's
        ``_pending_own_cancels`` never had this msg_id, so it applies only
        the removal."""
        data = event.data or {}
        msg_id = data.get("msg_id")
        seq = data.get("seq", 0)
        applied = self._queue_view.apply_inbox_cancel(msg_id=msg_id, seq=seq)
        if not applied or not msg_id:
            return
        self._sent_queue.remove_item(msg_id)
        self._queue_item_meta.pop(msg_id, None)
        cancelled_text = self._pending_own_cancels.pop(msg_id, None)
        if cancelled_text is not None:
            self._restore_cancelled_text(cancelled_text)

    def _handle_agent_delta_event(self, event) -> None:
        """Coalesce one streamed content-delta chunk into a SINGLE FlowView
        entry per reply (#3288 ③c — the L7 consumer this arc's ③b/③d phases
        deliberately left unbuilt: ③b's own gate is "an ``agent_delta`` with
        no consumer draws nothing", and this method IS that consumer,
        landing now).

        Correlates on ``chain_id`` — the authoritative id ``RouterLoop.
        _emit_agent_delta`` stamps on every delta and the terminal
        ``kind="agent"`` OutboxMessage carries in its own meta — never a
        guessed key (text-match correlation was tried and reverted earlier
        in this arc, #3309).

        First delta for a ``chain_id``: appends ONE new ``kind="agent"``
        flow entry seeded with that delta's text (renders through the SAME
        presenter path a terminal agent reply does — plain markdown body,
        no special-cased streaming style). Every SUBSEQUENT delta for the
        SAME ``chain_id`` accumulates onto the tracked text and updates
        that SAME entry in place (``Entry.set_item`` — bumps the revision,
        re-presents on the next viewport paint), never appending a second
        row. This entry is finalized (and popped from
        :attr:`_streaming_replies`) by the terminal completion frame in
        :meth:`_ingest_frame`, never here — so a chain_id's tracked partial
        never contests with the authoritative completed text (L9
        whole-persist stays the completion's job).

        A late-joining connection that only receives the TAIL of a stream
        (the mid-stream-join case #3288 ③d proved on the wire, handed off
        to ③c to close on the render side) is handled by this SAME code
        path: whichever deltas this client actually receives seed/update
        ONE entry exactly as above, and the terminal completion finalizes
        it — no separate branch needed, since the coalesce is keyed by
        chain_id, not by "have we seen the FIRST delta".

        Best-effort: a malformed/empty delta (no ``chain_id`` or empty
        ``text``) is silently dropped rather than crashing the pump — the
        emitting side (``RouterLoop``) never emits invalid deltas, so this
        is defensive only.
        """
        data = event.data or {}
        chain_id = data.get("chain_id")
        text = str(data.get("text", ""))
        if not chain_id or not text:
            return
        existing = self._streaming_replies.get(chain_id)
        if existing is None:
            from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

            entry = self.conversation.append(
                OutboxMessage(kind="agent", text=text, meta={"chain_id": chain_id})
            )
            self._streaming_replies[chain_id] = (entry, text)
            return
        entry, accumulated = existing
        accumulated += text
        entry.set_item(replace(entry.item, text=accumulated))
        self._streaming_replies[chain_id] = (entry, accumulated)

    def _restore_cancelled_text(self, text: str) -> None:
        """Restore a cancelled submission's text into the composer (#3300
        Y-client, owner-ratified detail): prepended at the HEAD even when the
        composer already holds a draft — a newline boundary separates the
        restored text from whatever was already there, so the draft survives
        intact rather than being clobbered. The cursor lands at the END of
        the restored text (never at the end of the whole, possibly longer,
        document), so continuing to type picks up right after the restored
        line(s) rather than after the user's own untouched draft.

        **Security**: the composer is a NEW render surface for this text
        (:attr:`RemoteQueueView.items` stores the RAW submission — neither
        ``apply_user_submitted`` nor this call's own path passes through
        ``SentQueue.show_item``'s neutralize, so nothing upstream has cleaned
        it yet). Same injection class as the sent-queue row itself (#3302):
        neutralized HERE, independently, before it ever reaches
        ``composer.text`` — a strip of this call's neutralize must leave the
        OTHER site (``SentQueue.show_item``) still clean and vice versa (two
        independent witnesses, no cross-masking)."""
        text = _neutralized_label(text)
        composer = self.query_one(Composer)
        existing = composer.text
        composer.text = f"{text}\n{existing}" if existing else text
        lines = text.split("\n")
        row = len(lines) - 1
        col = len(lines[-1])
        composer.move_cursor((row, col))

    async def on_sent_queue_cancelled(self, event: "SentQueue.Cancelled") -> None:
        """The user cancelled a queued row (:class:`SentQueue`'s ``Enter``
        binding, #3300 Y-client). Captures the item's CURRENT text from the
        queue view before issuing the cancel — the source the canceller-local
        restore uses once (if) the matching ``inbox_cancel`` delta actually
        arrives (:meth:`_handle_inbox_cancel_event`); the row removal and the
        restore are BOTH driven by that delta, never by this call's return
        value. The return value is used ONLY for pending-entry hygiene: a
        cancel that raced an already-dispatched item is a server no-op with
        NO delta ever following it, so nothing would ever pop this entry —
        pruning it here is memory hygiene, not a correctness dependency (the
        entry is keyed by a unique msg_id that is never reused, so a
        transient leftover entry can never cause a wrong future restore)."""
        msg_id = event.msg_id
        text = next(
            (
                item.get("text")
                for item in self._queue_view.queue()
                if item.get("msg_id") == msg_id
            ),
            None,
        )
        if text is not None:
            self._pending_own_cancels[msg_id] = text
        try:
            removed = await self._transport.cancel_queued(msg_id)
        except Exception:
            logger.exception("textual chat: cancel_queued failed")
            self._pending_own_cancels.pop(msg_id, None)
            return
        if not removed:
            self._pending_own_cancels.pop(msg_id, None)

    async def _pump_frames(self) -> None:
        """Drain the transport frame stream into the retained model.

        Display frames fold into the model (skipping command-UI sentinels) via
        :meth:`_ingest_frame` — appending a new entry, or COALESCING a correlated
        tool result into its RUNNING started entry (② settle-in-place). ``__end__``
        stops the app (the session closed). Event frames are the working-indicator
        path — consumed but not drawn, EXCEPT for the turn-end subset
        (:data:`_TURN_END_EVENT_TYPES`), which triggers
        :meth:`_sweep_orphaned_running_tools` (#72: force-settle any tool still
        RUNNING when its turn ends — a confirmed orphan).

        #3288 ③c: ``agent_delta`` (:meth:`_handle_agent_delta_event`) coalesces
        streamed reply chunks into ONE flow entry per ``chain_id`` — see that
        method's docstring and :attr:`_streaming_replies`. The entry it
        maintains is finalized by the terminal ``kind="agent"`` DISPLAY frame
        in :meth:`_ingest_frame`, never appended a second time.

        #3300 P2b/Y-client: ``user_submitted`` (:meth:`_handle_user_submitted_event`),
        ``turn_started`` (:meth:`_handle_turn_started_event`), and
        ``inbox_cancel`` (:meth:`_handle_inbox_cancel_event`) drive the
        sent-queue "upward conveyor" — materialize into the sent-queue region,
        then EITHER promote to a flow entry on dispatch OR remove on cancel
        (mutually exclusive per the server's atomic guarantee, issue #3300
        §6a). This REPLACES P1 C's "append the user_submitted echo straight
        to the flow": a submission now stages in the sent-queue first
        (near-instant promotion on an idle server, durably visible — and
        cancelable — while queued on a busy one). The queue model is seeded
        once, on the first frame (:meth:`_seed_queue_view`). A single frame's
        failure must not kill the pump, so ingest is guarded.
        """
        try:
            async for frame in self._transport.frames():
                if not self._queue_seeded:
                    try:
                        self._seed_queue_view()
                    except Exception:
                        logger.exception("textual chat: queue-view seed failed")
                    self._queue_seeded = True
                if frame.tag is FrameTag.EVENT:
                    etype = getattr(frame.event, "type", None)
                    if etype == "user_submitted":
                        try:
                            self._handle_user_submitted_event(frame.event)
                        except Exception:
                            logger.exception(
                                "textual chat: user_submitted ingest failed"
                            )
                    elif etype == "turn_started":
                        try:
                            self._handle_turn_started_event(frame.event)
                        except Exception:
                            logger.exception(
                                "textual chat: turn_started queue-promote failed"
                            )
                    elif etype == "inbox_cancel":
                        try:
                            self._handle_inbox_cancel_event(frame.event)
                        except Exception:
                            logger.exception(
                                "textual chat: inbox_cancel ingest failed"
                            )
                    elif etype == "agent_delta":
                        try:
                            self._handle_agent_delta_event(frame.event)
                        except Exception:
                            logger.exception(
                                "textual chat: agent_delta coalesce failed"
                            )
                    elif etype in _TURN_END_EVENT_TYPES:
                        try:
                            self._sweep_orphaned_running_tools()
                        except Exception:
                            logger.exception(
                                "textual chat: orphaned-tool sweep failed"
                            )
                    continue
                msg = frame.message
                if msg.kind == "__end__":
                    break
                if msg.kind in _SKIP_KINDS:
                    continue
                try:
                    self._ingest_frame(msg)
                except Exception:
                    logger.exception(
                        "textual chat: frame ingest failed for kind=%r", msg.kind
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
        """Route one submitted line through the transport send seam as an
        ordinary NEW turn.

        #3299 P1: the Composer is now EXCLUSIVELY for new turns — it no longer
        reads ``pending_intervention_head()`` at all. Answering a pending
        intervention (closed-set select or free-text) happens ONLY through the
        :class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
        (:meth:`on_intervention_panel_choice_selected` /
        :meth:`on_intervention_panel_text_submitted`), never here. A Composer
        submit that lands while an intervention is pending is NOT black-holed:
        the backend turn stays busy awaiting the intervention, so
        ``submit_user_text`` durably queues the line on the inbox — visible in
        the sent-queue region (#3300 P2b, this module) and cancelable there
        (#3300 Y-client, ``↑`` from the composer to focus it, ``Enter`` on a
        highlighted row to cancel) — rather than losing it. This delegation
        was verified live (turn-owner-task /
        inbox-durability trace) by the architect design pass for #3299, so P1
        needs no hint/block guard of its own. Errors are contained and
        surfaced as an error frame the pump renders — a silent input drop is
        the worst failure for a chat box."""
        try:
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
