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
that is stopped when the tool completes (Phase ②, #3283). A STREAMING reply's
live updates are likewise viewport-gated (Phase ③, #3283): the deltas always
accumulate, but the row is only re-rendered while it is on screen —
``FlowView.track_visibility`` replays the accumulated text in one update if the
row scrolls back, so scrolling away never truncates a reply.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` / ``textual_flowview``
imports never reach an always-loaded module.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, OptionList, Static
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

from ._meta_keys import ELAPSED_SECS_KEY as _ELAPSED_SECS_KEY
from ._meta_keys import ORPHANED_RESULT_KIND as _ORPHANED_RESULT_KIND
from .chrome import (
    _MENU_TABS,
    Composer,
    MenuBar,
    StatusLine,
    _history_option_content,
    build_drawer_pane,
    pane_commands,
    pane_payload,
    status_line_text,
)
from .gutter import (
    _RUNNING_FRAME_PERIOD,
    RIGHT_GUTTER_WIDTH,
    ReynGutter,
    ReynRightGutter,
)
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
    from textual_flowview import VisibilityHandle

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


@dataclass(slots=True)
class _StreamingReply:
    """One in-flight streamed reply, keyed by ``chain_id`` in
    :attr:`TextualChatApp._streaming_replies` (#3288 ③c, visibility-gated by
    #3283 ③).

    Separates the two things a streamed reply needs to keep apart:

    - :attr:`text` — the FULL accumulated reply. Authoritative, appended to on
      EVERY delta unconditionally, whether or not the row is on screen. Nothing
      about visibility may ever skip this line; the deferral below is a render
      optimisation, never a data path.
    - :attr:`rendered` — how much of :attr:`text` the flow entry has actually
      been handed (via ``Entry.set_item``). Equal to :attr:`text` while the row
      is on screen; LAGS it while the row is scrolled out of view.

    ``rendered != text`` is therefore exactly "this row owes the viewport a
    repaint", and :meth:`TextualChatApp._flush_streaming_reply` is the only
    thing that closes the gap — driven either by the next delta (while visible)
    or by ``on_show`` when the row scrolls back (while it was not).

    :attr:`handle` is the ``FlowView.track_visibility`` registration whose
    ``on_show``/``on_hide`` maintain :attr:`visible`; :meth:`release` stops it
    idempotently (a second call, or a call after the entry was removed / the
    model cleared, is a no-op — flowview's ``VisibilityHandle.stop`` already
    returns early once its observer is gone)."""

    entry: "Entry[OutboxMessage]"
    text: str
    rendered: str
    visible: bool = True
    handle: "VisibilityHandle | None" = None

    @property
    def pending(self) -> bool:
        """Whether accumulated text is waiting on a repaint (deferred while the
        row is off screen). The public read the ③ gates witness."""
        return self.rendered != self.text

    def release(self) -> None:
        """Unregister the visibility tracker. Idempotent — safe to call on
        completion, on session switch, and twice."""
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.stop()


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
    from ``model_classes`` / ``session_tree`` (the enumerating pickers derive their
    FULL set from the registry, never a curated subset), Cost/Ctx from the live
    token/cost/context figures (F5b — the headline cost + ctx% is also on the
    always-visible status line), Tool/MCP/Skill/Hook from the session-scoped
    visibility + hook toggles, Pipe/Cron from the pipeline registry + cron config,
    Menu from the slash ``REGISTRY``, History from the retained live conversation,
    Help from the app BINDINGS. Selecting an actionable row routes that row's
    slash (``/model`` / ``/attach`` / ``/session switch`` / ``/visibility`` /
    ``/hook``) through the transport — the command comes from the SAME per-pane
    entry list that produced the row, so index and action cannot drift.

    #3338 makes both of those surfaces LIVE rather than sampled-once:
    :meth:`_refresh_live_chrome` runs on EVERY frame — EVENT as well as DISPLAY —
    and rebuilds the status line plus whichever pane is currently OPEN. Restricting
    the rebuild to the open tab is load-bearing, not an optimization: the Ctx pane
    resolves the deliberately-uncalled ``ctx_compaction_status_fn``, which
    ``_snapshot()`` stores as a bound method precisely so it never runs per render
    frame.

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

    #3310 N2 reuses that SAME hydrate seam for a session SWITCH, not just a
    restart. N1 (#3321) added a ``session_attached`` chat-event — an
    ``EventFrame`` the registry puts directly on ``repl_outbox`` at the attach
    seam, with NO ``await`` between the ``self._attached`` flip and the put —
    so it is a stream BARRIER: everything on the frame stream before it
    belongs to the OLD attached session, everything after to the NEW one, by
    construction. :meth:`_handle_session_attached_event` consumes it: a cached
    FlowView cannot be the source of truth here (while THIS client was on some
    OTHER session, the registry forwarder DROPPED this session's frames
    entirely — a cache would be missing everything that happened meanwhile
    and would hold tool rows stuck RUNNING), so the response is reconnect-
    shaped, not cache-shaped — reset EVERY per-session client state
    (conversation, running-tool tracking, pending-intervention tabs, the
    sent-queue view/widget, streaming-reply tracking) and rehydrate the model
    from the NEW session's ``history.jsonl`` via :meth:`_hydrate_from_history`,
    now generalized to target an arbitrary ``(agent, session_id)`` instead of
    only "whichever session is attached". A tool that completed while
    detached resolves correctly (never RUNNING) because the restore path
    always projects a resolved state; a pending intervention for the NEW
    session is not re-fetched here — the registry already re-announces it on
    attach, so the client only has to forget the OLD one.

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
    Composer WITHOUT answering — no intervention's state changes. The
    Composer itself is EXCLUSIVELY for new turns — it no longer reads
    ``pending_intervention_head()`` at all — with ONE narrow exception,
    ``/answer`` (#3327, see :meth:`_submit`).

    **#3327 correction of an earlier, FALSIFIED claim in this docstring**:
    an older revision asserted that Esc/Tab needed "no black-hole guard…the
    #3300 sent-queue durably holds any new Composer submit while any stay
    pending." That is true as far as it goes but misses the actual failure
    mode: the #3300 sent-queue durably HOLDS a queued submit, but only
    DISPATCHES it once the blocking turn frees — which for an ``/answer``
    aimed at THAT SAME pending intervention can never happen (the turn frees
    only once the intervention resolves, and the intervention resolves only
    once the queued ``/answer`` dispatches — a chicken-and-egg deadlock, not
    a "durably held, eventually delivered" queue wait). A keyboard-only user
    who ``Esc``-dismissed the panel — the escape hatch above, still intended
    and unchanged — had no way back: ``Tab``/``Shift+Tab`` only cycle
    Composer↔MenuBar, and the Composer's own ``↓``/``↑`` targeted the menu
    and sent-queue, never the panel. Two fixes close this, both #3327: (1)
    :meth:`_submit` now tries ``/answer`` through
    :meth:`~reyn.interfaces.transport.client_transport.ClientTransport.deliver_pending_answer`
    — a DIRECT, un-queued delivery — before the queued path, so it can
    always resolve the intervention it targets regardless of turn state; (2)
    the Composer's ``↑`` (first line, per :class:`Composer`'s own
    ``_on_key``) now focuses the pending :class:`InterventionPanel` FIRST,
    ahead of the sent-queue, whenever one is showing — the SAME idiom that
    already routes ``↑`` to the sent-queue, extended rather than replaced,
    and registered in :data:`~reyn.interfaces.inline.textual_chat.chrome.COMPOSER_KEYS`
    so the Help pane surfaces it.
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
    /* height: auto — the menu row WRAPS to as many lines as the terminal width
       needs (chrome.pack_menu_rows), so no tab is ever laid out past the right
       edge. A fixed height:1 here would clip the wrapped rows straight back
       off-screen, reinstating exactly the defect the wrap exists to fix.
       THIS RULE IS THE SOLE OWNER of the row's height: an app stylesheet beats
       a widget's DEFAULT_CSS, so declaring height on MenuBar in chrome.py has
       no effect (measured). Change it here. */
    MenuBar {
        height: auto;
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

    #: Per-session ``dict``-valued client state that resets uniformly (a
    #: plain ``.clear()``) on a session switch (#3310 N2,
    #: :meth:`_handle_session_attached_event`). Declared here — never
    #: enumerated ad hoc inside the reset method — so a FUTURE per-session
    #: dict added to :meth:`__init__` is reset BY CONSTRUCTION the moment
    #: its name is added to this tuple, rather than by a human remembering
    #: to also touch the reset method. This is not a hypothetical: the
    #: exact omission class hit TWICE in this arc — ``_streaming_replies``
    #: was flagged as a likely-forgotten addition during the #3310 design
    #: pass (#3288 ③c landed after the design table was written), and
    #: ``_pending_own_cancels`` (#3300 Y-client) was found missing from
    #: that SAME design table only while writing this PR's gates. State
    #: that needs a NON-``.clear()`` reset (a fresh instance, a widget
    #: method, a follow-on hydrate) stays explicit in the reset method
    #: below — this tuple covers only the "just empty the dict" shape.
    #:
    #: ★``_streaming_replies`` is BOTH: its records are dropped by this
    #: tuple's uniform ``.clear()``, but each record also OWNS a released
    #: resource (its #3283 ③ ``FlowView.track_visibility`` handle), which a
    #: ``.clear()`` cannot express. The reset method therefore releases those
    #: handles explicitly, before the loop — registering a future dict here
    #: still resets it, but a future dict whose VALUES own a resource needs
    #: that same explicit release too.
    _PER_SESSION_DICT_STATE: "tuple[str, ...]" = (
        "_running_tools",
        "_pending_ivs",
        "_queue_item_meta",
        "_streaming_replies",
        "_pending_own_cancels",
    )

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
        # Per-picker parallel SLASH COMMAND lists, keyed by tab id and kept in
        # lock-step with the OptionList options a pane was last refreshed with, so
        # an ``OptionSelected.option_index`` maps back to the command that applies
        # that row (``/model`` / ``/attach`` / ``/session switch`` /
        # ``/visibility`` / ``/hook``). Populated on each drawer refresh
        # (:meth:`_refresh_pane`) from the SAME snapshot that built the rows, via
        # the SAME per-pane entry list (``chrome._PANE_ENTRY_BUILDERS``), so the
        # option row and its command never drift.
        self._pane_commands: "dict[str, list[str]]" = {}
        # #3288 ③c: in-flight streamed reply, keyed by ``chain_id`` — the SAME
        # authoritative correlation id ``RouterLoop._emit_agent_delta`` stamps
        # on every ``agent_delta`` chat-event AND the one the terminal
        # ``kind="agent"`` OutboxMessage carries in its ``meta["chain_id"]``
        # (never a guessed key — text-match correlation was tried and
        # reverted earlier in this arc, issue #3288/#3309). Each value is a
        # :class:`_StreamingReply`: the FIRST delta for a chain_id appends ONE
        # new flow entry; every SUBSEQUENT delta for that SAME chain_id
        # accumulates onto that record and updates that SAME entry in place
        # (:meth:`_handle_agent_delta_event`) rather than appending a second
        # row. #3283 ③: the in-place update is VISIBILITY-GATED — the text
        # always accumulates, but the entry is only handed it while the row is
        # on screen (``FlowView.track_visibility``, replayed by ``on_show``).
        # The terminal completion (:meth:`_ingest_frame`'s ``kind == "agent"``
        # branch) releases the visibility tracker, pops the record and
        # finalizes the entry with the authoritative full text — the ONLY
        # place a streamed reply's entry is removed from this map outside a
        # session switch, so a chain_id can never leak past its turn's
        # completion.
        self._streaming_replies: "dict[str, _StreamingReply]" = {}
        # #3283 ④: the status snapshot's keyed per-turn token/cost lookup
        # (``turn_usage_fn`` = ``Session.turn_usage``), cached off the snapshot
        # :meth:`_refresh_live_chrome` already reads once per arriving frame.
        # The right gutter calls it once PER RENDERED ROW, so it must not build
        # a snapshot itself; and it must not hold the bound method forever
        # either (a session SWITCH rebinds it), hence re-cached per frame from
        # the same read rather than resolved once at mount. ``None`` until the
        # first frame, and permanently ``None`` on a remote client (per-turn
        # buckets are session-local, not on the wire) — the gutter renders
        # ``—`` for a row whose turn it cannot price, never a fabricated 0.
        self._turn_usage_fn: "Callable[[str], dict | None] | None" = None
        # One-shot latch so a raising lookup is logged once, not once per
        # rendered row per repaint (see :meth:`_turn_usage`).
        self._turn_usage_lookup_failed = False

    def compose(self) -> ComposeResult:
        # Held so the frame pump can start/stop the per-entry BODY animation
        # (``animate_entry``/``stop_entry_animation``) that drives a RUNNING tool
        # row's live spinner + elapsed (Phase ②).
        self._flow: "FlowView[OutboxMessage]" = FlowView(
            model=self.conversation,
            presenter=self._presenter,
            decorator=ReynGutter(frame_period=_RUNNING_FRAME_PERIOD),
            gutter_width=_GUTTER_WIDTH,
            # Phase ④ (#3283): the RIGHT gutter shows per-entry elapsed time
            # (tool rows) AND the row's turn's real prompt/completion token
            # split (agent reply rows, via the keyed per-turn lookup) — see
            # ReynRightGutter and its two halves for the content-set decisions.
            # additive flowview params; the LEFT gutter/state contract above is
            # untouched.
            right_decorator=ReynRightGutter(
                clock=self._clock, usage_lookup=self._turn_usage
            ),
            right_gutter_width=RIGHT_GUTTER_WIDTH,
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
        yield MenuBar(_MENU_TABS, id="menubar")
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

    def _turn_usage(self, chain_id: str) -> "dict | None":
        """The real per-turn figures for ``chain_id``, or ``None`` when there
        is no figure — the right gutter's per-row lookup (#3283 ④). The gutter
        draws the prompt/completion token split; the dict also carries the
        turn's USD cost for any other caller.

        Delegates to the snapshot's ``turn_usage_fn``
        (:attr:`_turn_usage_fn` = ``Session.turn_usage``, keyed over
        ``BudgetTracker``'s bounded per-turn buckets). ``None`` covers every
        no-figure case uniformly — no read model / pre-first-frame, a remote
        client (per-turn buckets are session-local), a turn that recorded no
        LLM spend, and a turn EVICTED from the buckets — and the gutter renders
        ``—`` for all of them. Never a fabricated ``0``, and never a figure
        derived from cumulative counters.

        A raising lookup is logged ONCE per app run, not once per row per
        repaint: this runs on the render path, so an unconditional
        ``logger.exception`` here would drown the log at frame rate for a
        single persistent fault."""
        fn = self._turn_usage_fn
        if fn is None:
            return None
        try:
            return fn(chain_id)
        except Exception:
            if not self._turn_usage_lookup_failed:
                self._turn_usage_lookup_failed = True
                logger.exception(
                    "textual chat: per-turn usage lookup failed "
                    "(right gutter will show '—'; logged once)"
                )
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

    def _status_text(self, snap: "dict | None | object" = _UNSET) -> str:
        """The status-values line (``model │ agent │ cost │ ctx``), from the live
        status snapshot (F5b: running cost + context percent are visible even with
        the drawer closed). Falls back to the threaded ``agent_name`` pre-session.
        Pass ``snap`` to reuse an already-read snapshot (one read per frame)."""
        snapshot = self._snapshot() if snap is _UNSET else snap
        return status_line_text(snapshot, self._agent_name)  # type: ignore[arg-type]

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

    def _hydrate_from_history(
        self, *, agent: "str | None" = None, session_id: "str | None" = None
    ) -> None:
        """Restore-on-restart (#3273 Phase 5) AND session-switch reset+rehydrate
        (#3310 N2): project a persisted conversation log into the retained
        model so the pane shows that session's PREVIOUS conversation instead
        of whatever was left over from before.

        Two call shapes:

        - No args (:meth:`on_mount`'s original Phase-5 call): hydrates the
          CURRENTLY ATTACHED session, byte-identical to pre-N2 behavior.
        - ``agent``/``session_id`` given (:meth:`_handle_session_attached_event`,
          called AFTER :meth:`self.conversation`'s ``clear()``): hydrates that
          SPECIFIC (possibly never-before-attached-in-this-client-run) session
          instead — the same read-model seam
          (:meth:`~reyn.interfaces.repl.read_model.ChatReadModel.conversation_history`
          — ``history.jsonl``, NOT the P6 audit-event log), just targeted.

        Appends each projected frame to ``self.conversation``
        (:func:`.restore.project_restored_frames`). Every restored entry is
        RESOLVED, never RUNNING: a restored tool turn is already projected into
        the SAME coalesced ``tool_call_started`` shape the live path's
        :meth:`_coalesce_tool_result` settles a completed tool into (call
        header + folded result, one entry — see
        ``restore.project_restored_frames``'s docstring), so this method just
        derives the terminal SUCCESS/ERROR state from the coalesced result
        (the ``if msg.kind == "tool_call_completed"`` branch a pre-coalesce
        restore shape would have hit no longer fires for tool rows; user/agent
        rows keep DEFAULT, their live state) — which is also what makes a tool
        that completed while this client was detached (#3310's "orphan gate")
        resolve correctly rather than replaying as RUNNING: the completion
        already landed in ``history.jsonl`` before this read, so it projects
        straight to its settled state. A REMOTE read model returns an empty log
        (frame-sufficiency: past turns are not on the wire) → this is a no-op
        either way, and the pane starts/stays blank (remote switch-rehydrate is
        #3310 N3's job). Fully guarded — a restore failure must never stop the
        app from mounting/resetting and pumping live frames."""
        if self._read_model is None:
            return
        try:
            if agent is not None or session_id is not None:
                messages = self._read_model.conversation_history(
                    agent=agent, session_id=session_id
                )
            else:
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

    def _refresh_pane(self, tab_id: str, snap: "dict | None | object" = _UNSET) -> None:
        """Re-derive ``tab_id``'s pane content from the current canonical sources
        and update the mounted widget in place (``OptionList`` options or the
        ``Static`` text). One snapshot read feeds BOTH the rows and the parallel
        slash commands, so an ``OptionSelected`` maps back to the right command.
        Pass ``snap`` to reuse an already-read snapshot.

        The History tab's rows get the SAME ``Content``-literal fidelity wrap
        :func:`~reyn.interfaces.inline.textual_chat.chrome.build_drawer_pane`
        applies at initial ``compose`` time (:func:`~reyn.interfaces.inline.
        textual_chat.chrome._history_option_content`) — this refresh path is a
        SEPARATE call site from that initial build (``OptionList.add_options``
        vs the constructor), so it needs its own, independently-verified wrap;
        the row TEXT itself is already neutralized upstream, in
        :meth:`_history_turns`."""
        snapshot = self._snapshot() if snap is _UNSET else snap
        rows = self._pane_rows(tab_id, snapshot)
        self._pane_commands[tab_id] = pane_commands(tab_id, snapshot)  # type: ignore[arg-type]
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
        """Apply a picked row by routing its slash command through the transport —
        the SAME ``/model <class>`` / ``/attach <name>`` / ``/session switch <sid>``
        / ``/visibility on|off <kind> <name>`` / ``/hook on|off <name>``
        slash-command contract the plain path dispatches. The command comes from
        the per-pane list :meth:`_refresh_pane` built alongside the rows
        (:func:`~reyn.interfaces.inline.textual_chat.chrome.pane_commands`), so the
        index can never address a different row's action. Non-actionable panes
        (History/Menu, and a category's read-only fallback listing) carry no
        command and just collapse. Then close the drawer and return focus to the
        composer."""
        tab_id = event.option_list.id
        cmds = self._pane_commands.get(tab_id or "", [])
        if 0 <= event.option_index < len(cmds) and cmds[event.option_index]:
            await self._submit(cmds[event.option_index])
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
        answering — the escape hatch of the focus lifecycle, unchanged by
        #3327. Every pending intervention stays exactly as it was (the panel
        stays open); a new Composer submit durably queues on the inbox
        rather than black-holing (#3300's sent-queue). #3327 fixed the
        REACHABILITY gap this escape hatch used to leave behind: a
        keyboard-only user now has a way back to the panel (the Composer's
        ``↑``, see :class:`Composer`'s ``_on_key``) and ``/answer`` no longer
        deadlocks behind the sent-queue (see :meth:`_submit`) — so dismissing
        here is a real, recoverable "focus elsewhere for now", not a
        one-way trip."""
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
        truth), pops the tracked chain_id and releases its #3283 ③
        visibility tracker (no observer outlives a settled row — and the
        settle write itself is NOT visibility-gated: the authoritative text
        lands even if the row is off screen). Every OTHER frame — including a
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
                # Release the ③ visibility tracker BEFORE the final write: the
                # record is already out of the map, so no callback could find it
                # anyway, and nothing is left registered on a settled row.
                streaming.release()
                # Unconditional — the completion's authoritative full text lands
                # whether or not the row is currently on screen. The ③ deferral
                # governs only the intermediate partials, never this settle.
                streaming.entry.set_item(
                    replace(streaming.entry.item, text=msg.text, meta=meta)
                )
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
        since = started_meta.get(_RUNNING_SINCE_KEY)
        if isinstance(since, (int, float)):
            # Capture the FINAL elapsed seconds (Phase ④, #3283) from the SAME
            # timestamp the live spinner was reading, before it is stripped
            # above — the right gutter's static elapsed for a settled row.
            merged[_ELAPSED_SECS_KEY] = max(0, int(self._clock() - since))
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
                orig_meta = entry.item.meta or {}
                meta = {
                    k: v for k, v in orig_meta.items() if k != _RUNNING_SINCE_KEY
                }
                meta[_RESULT_KIND_KEY] = _ORPHANED_RESULT_KIND
                since = orig_meta.get(_RUNNING_SINCE_KEY)
                if isinstance(since, (int, float)):
                    # Same final-elapsed capture as _coalesce_tool_result — an
                    # orphan still ran for a real, observed duration before
                    # being force-settled (Phase ④, #3283).
                    meta[_ELAPSED_SECS_KEY] = max(0, int(self._clock() - since))
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

    def _handle_session_attached_event(self, event) -> None:
        """The session-switch reset barrier (#3310 N2, consuming N1's
        ``session_attached`` chat-event, ``{agent, session_id}``).

        ★Design thesis (architect deep-dive, issue #3310 §1): a cached
        FlowView cannot be the source of truth after a switch — while THIS
        session was detached, the registry forwarder DROPPED its frames
        (``registry.py``'s "durable narration is in history.jsonl" branch),
        so any client-side cache is stale-by-construction (missing frames,
        tool rows stuck RUNNING). v1 is therefore reconnect-shaped, not
        cache-shaped: reset EVERY per-session client state, then rehydrate
        from the durable sources exactly like a fresh reconnect would
        (:meth:`_hydrate_from_history`, the #3305-shaped queue reseed below).

        Resets, one independent state at a time (per-state test coverage,
        #3310 gate 3 — folding several clears into one test would repeat the
        #3302/#3308 sibling-guard-site mistake this repo already learned
        from):

        - ``self.conversation`` (the retained FlowModel) — cleared, then
          rehydrated from the NEW session's ``history.jsonl`` below. This is
          the ★staleness-gate state: the frames the OLD session produced
          while this client was on some OTHER session are NOT re-derived
          from an in-memory cache (there is none) — they come back because
          they are durable, not because anything was retained live.
        - :attr:`_running_tools` — a RUNNING marker is per-session op-id
          state; the new session's own in-flight tools (if any) are not
          replayed here (frame-sufficiency: a truly still-running tool has
          no completed row in ``history.jsonl`` yet either — same rule live
          frames already follow for a mid-flight tool this client just
          joined).
        - :attr:`_pending_ivs` — forgotten, not re-fetched: the server side
          already re-announces every pending intervention on attach
          (``registry.py``'s ``attach``/``attach_session``, both replay
          ``_interventions.list_active()`` — ground-truthed during the N1
          design pass), so the client only needs to stop tracking the OLD
          session's entries; the new ones arrive as ordinary ``intervention``
          frames right after this barrier. Paired with
          :meth:`InterventionPanel.collapse_all` so every OLD tab (post-P5
          the panel is tabbed, #3308) actually closes rather than lingering
          empty.
        - :attr:`_queue_view` / :attr:`_queue_seeded` — a FRESH
          ``RemoteQueueView()``, immediately re-seeded from the NEW session's
          OWN snapshot right here (:meth:`_seed_queue_view`) rather than
          deferred to "whenever the next frame happens to arrive" — the
          identical PROJECTION the #3305 reconnect-reseed/mount-time seed
          use, just called eagerly instead of gated on
          :attr:`_queue_seeded`. ★Found during gate-writing (not in the
          architect's table): deferring to the generic first-frame gate
          (leaving ``_queue_seeded = False`` and letting the ordinary
          "seed on first frame" check in :meth:`_pump_frames` catch it) is
          NOT equivalent here — that check runs BEFORE a frame is
          dispatched, so it would need a frame AFTER this barrier to ever
          fire; a session with an already-queued item but no OTHER pending
          activity would show an empty sent-queue region until something
          else happened to arrive. Seeding eagerly here closes that gap;
          :attr:`_queue_seeded` is still set True (never left False) so the
          generic check is correctly a no-op for this same barrier frame.
        - :attr:`_queue_item_meta` — cleared (keyed by msg_id, which is
          per-submission; an old session's entries are meaningless once its
          queue view is gone too).
        - the :class:`SentQueue` widget's rows — :meth:`SentQueue.clear_all`
          (a new method this PR adds; the widget had no server delta of its
          own for "the whole displayed session changed").
        - :attr:`_streaming_replies` (#3288 ③c) — ★explicitly flagged by the
          architect as the state most likely to be forgotten by a LATER
          phase; cleared here since it already exists. #3283 ③ made that
          prediction concrete: each record now owns a
          ``FlowView.track_visibility`` handle, so the clear is preceded by
          an explicit release loop, releasing what this app acquired rather
          than leaning on ``FlowView.on_flow_clear`` also dropping every
          observer (it does — see the inline note at that loop).
        - :attr:`_pending_own_cancels` (#3300 Y-client) — an addition beyond
          the architect's enumerated table (found verifying it against this
          file): keyed by msg_id and normally popped once the matching
          ``inbox_cancel`` delta confirms a client-issued cancel. A switch
          before that confirmation would otherwise leave a stale entry that
          could later restore OLD-session cancelled text into the composer
          if a delta for that same msg_id ever arrived under a DIFFERENT
          attached session — clearing it here removes that (admittedly
          narrow) cross-session leak.
        - :attr:`_pane_commands` is intentionally NOT reset here — the
          architect's design pass confirmed it is fine as-is: each drawer
          pane rebuilds it from a fresh snapshot on every open
          (:meth:`_refresh_pane`), so a stale entry can never be read before
          being overwritten.
        - ``_iv_current_key`` (named in the issue's original enumeration) no
          longer exists in this file: #3308 (P5) replaced the single-slot
          re-route it belonged to with per-intervention TABS, so
          :attr:`_pending_ivs` + :meth:`InterventionPanel.collapse_all` are
          the whole of the intervention reset today.

        ★Structural fix (co-vet review, #3323): a hand-typed list of
        ``.clear()`` calls has now been forgotten TWICE in this arc's short
        history — ``_streaming_replies`` (flagged as at-risk by the design
        pass) and ``_pending_own_cancels`` (found only while writing this
        PR's gates) both landed in ``__init__`` without their owning design
        table being updated. Every plain-``.clear()``-shaped state above
        (``_running_tools`` / ``_pending_ivs`` / ``_queue_item_meta`` /
        ``_streaming_replies`` / ``_pending_own_cancels``) is therefore
        declared ONCE, in :attr:`_PER_SESSION_DICT_STATE`, and this method
        iterates that tuple instead of re-listing each name — adding a
        FUTURE per-session dict to ``__init__`` and this tuple is now the
        only step required; there is no second call site to forget. State
        needing a non-``.clear()`` reset (``_queue_view``'s fresh instance,
        the panel/widget's own methods, the hydrate call) stays explicit
        below, since a uniform loop cannot express those shapes.

        Runs the reset UNGUARDED (a `try`/`except` around clearing plain
        dicts/lists would only hide a real bug) but the follow-on hydrate
        call is internally guarded, same as the mount-time call."""
        data = event.data or {}
        agent = data.get("agent")
        session_id = data.get("session_id")
        # #3283 ③: release every in-flight streamed reply's visibility tracker
        # BEFORE the model is cleared, so no ``on_show``/``on_hide`` callback of
        # the OLD session's rows is still registered on the flow. This is the one
        # per-session state whose reset is not fully expressible as ``.clear()``
        # (see :attr:`_PER_SESSION_DICT_STATE`) — the dict clear below drops the
        # records, this loop releases the resource each record OWNS. Idempotent
        # (:meth:`_StreamingReply.release`).
        #
        # ★Honest scope: for THIS path the loop is belt-and-braces — stripping it
        # changes no observable behaviour, because ``FlowModel.clear`` below
        # reaches ``FlowView.on_flow_clear``, which drops every observer itself
        # (a SECOND declaration of the same release, in the library). The app
        # releases what the app acquired rather than depending on that; the
        # load-bearing release is the one in :meth:`_ingest_frame`, on the
        # completion path, where nothing else would ever unregister a settled
        # row's observer (they would accumulate for the whole session — the very
        # scale problem ③ exists to remove).
        for reply in self._streaming_replies.values():
            reply.release()
        self.conversation.clear()
        # Data-driven over :attr:`_PER_SESSION_DICT_STATE` — see that
        # attribute's docstring — instead of a hand-written list of
        # ``.clear()`` calls, so a FUTURE dict-valued per-session addition
        # is reset the moment it is registered there, not only when a
        # human also remembers to edit this method.
        for attr_name in self._PER_SESSION_DICT_STATE:
            getattr(self, attr_name).clear()
        self._iv_panel.collapse_all()
        self._queue_view = RemoteQueueView()
        self._sent_queue.clear_all()
        if agent:
            self._agent_name = agent
        # Eager reseed (see the ``_queue_view``/``_queue_seeded`` bullet
        # above): seed the fresh view from the NEW session's snapshot right
        # now, rather than deferring to the generic "first frame" check —
        # which would need ANOTHER frame after this barrier to ever fire.
        try:
            self._seed_queue_view()
        except Exception:
            logger.exception("textual chat: switch queue-view reseed failed")
        self._queue_seeded = True
        self._hydrate_from_history(agent=agent, session_id=session_id)

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

    def _handle_intervention_answer_event(self, event) -> None:
        """Render an ``intervention_answer_submitted`` chat-event as a flow
        entry (#3300 — the last outbox `kind="user"` broadcast site,
        ``InterventionHandler.deliver_answer_to``, migrated to a chat-event).

        Unlike ``user_submitted`` (which stages in the sent-queue region
        first, :meth:`_handle_user_submitted_event`), an intervention answer
        has no queue/dispatch lifecycle to stage through — it renders
        straight to the flow, same as before this event-ify (when it arrived
        as a DISPLAY frame). The payload carries RAW text; this is the
        surface's OWN neutralize-at-render-boundary call (the SAME
        ``_neutralized_label`` seam :meth:`_handle_turn_started_event` uses
        for the analogous ``user_submitted`` promotion), so a control/ESC
        byte in an answer (free-text or an LLM-derived choice label) cannot
        reach this TTY.
        """
        from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

        data = event.data or {}
        text = _neutralized_label(str(data.get("text", "")))
        meta = dict(data.get("meta") or {})
        self._ingest_frame(OutboxMessage(kind="user", text=text, meta=meta))

    def _handle_session_halted_event(self, event) -> None:
        """#2280: the durability-halt observability surface. ``session_halted``
        (``Session._fail_stop_if_durability_dead`` / ``run_one_iteration``)
        fires the MOMENT the fail-stop latches — including while the operator
        is fully idle, no DISPLAY frame in flight — so this is the ONE handler
        that must call :meth:`_refresh_status` OUTSIDE the normal "a DISPLAY
        frame landed" trigger (see the F5b refresh-per-message-frame comment in
        :meth:`_pump_frames`): without it, an idle operator's status line would
        stay stale until the next unrelated message happened to land. The
        status snapshot itself already carries ``halted_reason`` live off
        ``Session.halted_reason`` (``interfaces/repl/status.py``'s
        ``_snapshot``) — this call is purely "go read it now", never a second
        source of truth. Purely observability: does not touch the halt itself,
        which is already enforced synchronously elsewhere."""
        self._refresh_status()

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

        **#3283 ③ — the in-place update is VISIBILITY-GATED.** The append
        registers a ``FlowView.track_visibility`` tracker for the entry
        (:meth:`_on_streaming_entry_shown` / :meth:`_on_streaming_entry_hidden`),
        and a subsequent delta hands the entry its new text only while the row
        is ON SCREEN. Off screen, the delta still accumulates onto
        :attr:`_StreamingReply.text` — always, unconditionally — but issues NO
        ``set_item``; the deferred text is replayed in ONE update when the row
        scrolls back into view. So a long conversation whose streaming reply has
        been scrolled away costs O(1) model→view updates instead of O(deltas),
        and scrolling back shows the COMPLETE reply, never a truncated one.

        This is a distinct gate from the one flowview already applies: flowview
        skips the *present + reflow* for an off-screen update
        (``FlowView.on_flow_update``), but the ``set_item`` itself — a new item
        object, a revision bump, a strip-cache eviction and a model→view
        notification per delta — happens regardless. ③ gates that *update feed*;
        flowview gates the *render*. Neither replaces the other, and neither is
        a correctness mechanism: strip this deferral and the reply is still
        complete, just updated once per delta.

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
            # The append already carries this first chunk, so rendered == text.
            record = _StreamingReply(entry=entry, text=text, rendered=text)
            self._streaming_replies[chain_id] = record
            self._track_streaming_visibility(record)
            return
        # ★Accumulate FIRST and unconditionally — the visibility gate below may
        # skip the RENDER, never this line. An off-screen reply that is never
        # re-shown before its completion still had every byte collected here.
        existing.text += text
        if existing.visible:
            self._flush_streaming_reply(existing)

    def _track_streaming_visibility(self, record: "_StreamingReply") -> None:
        """Bind a streamed reply's live-update feed to its row's viewport state
        (#3283 ③, ``FlowView.track_visibility``).

        The two callbacks are BOUND METHODS, not per-chain closures, and they
        recover their ``chain_id`` from the entry's own item meta — the SAME
        authoritative key :attr:`_streaming_replies` is filed under, which every
        write to this entry preserves (``replace`` keeps ``meta``, and the
        terminal completion's meta carries the chain_id too). So nothing here
        captures a chain_id, a record, or the app in a closure that could outlive
        the tracker.

        ``track_visibility`` fires ``on_show`` SYNCHRONOUSLY when the entry is
        already on screen, which is the ordinary case for a fresh streamed reply
        under ``STICKY_BOTTOM`` — so :attr:`_StreamingReply.visible` starts out
        agreeing with the viewport rather than being assumed. When the flow is
        not yet laid out (zero content width) flowview reports NOTHING visible
        and ``on_show`` does not fire; the record's ``visible=True`` default then
        keeps the feed live (updates are cheap and flowview drops the off-screen
        render itself) instead of silently deferring forever.

        Fully guarded, same as ② 's :meth:`_begin_running_indicator`: the gate is
        an OPTIMISATION, so failing to register it must degrade to "update on
        every delta" — never break the pump or lose a chunk."""
        try:
            record.handle = self._flow.track_visibility(
                record.entry,
                on_show=self._on_streaming_entry_shown,
                on_hide=self._on_streaming_entry_hidden,
            )
        except Exception:
            logger.exception("textual chat: could not track streamed-reply visibility")

    def _streaming_record_for(self, entry: "Entry[OutboxMessage]") -> "_StreamingReply | None":
        """The tracked record for ``entry``, or ``None`` if it is no longer
        in flight (already finalized, or reset by a session switch) — the lookup
        both visibility callbacks share, keyed by the entry's own
        ``meta["chain_id"]``. Identity-checked: a record found under that
        chain_id but pointing at a DIFFERENT entry is not this entry's, so a
        stale callback can never write into a successor's row."""
        chain_id = (entry.item.meta or {}).get("chain_id")
        if not chain_id:
            return None
        record = self._streaming_replies.get(chain_id)
        if record is None or record.entry is not entry:
            return None
        return record

    def _on_streaming_entry_shown(self, entry: "Entry[OutboxMessage]") -> None:
        """★The replay leg (#3283 ③): a streamed reply's row scrolled back INTO
        view — hand the entry everything that accumulated while it was away, in
        ONE update.

        This is the leg that makes the deferral safe. Strip it and the deferral
        becomes data loss on screen: the row would keep showing whatever text it
        had when it scrolled away, and the deltas that arrived while off-screen
        would never reach it until (if ever) the terminal completion frame
        overwrote the whole body. The accumulated text itself is never at risk
        (:meth:`_handle_agent_delta_event` appends unconditionally); this call is
        what puts it on screen."""
        record = self._streaming_record_for(entry)
        if record is None:
            return
        record.visible = True
        if record.pending:
            self._flush_streaming_reply(record)

    def _on_streaming_entry_hidden(self, entry: "Entry[OutboxMessage]") -> None:
        """A streamed reply's row left the viewport (#3283 ③) — stop feeding it
        updates. Deltas keep accumulating; only the ``set_item`` is deferred,
        until :meth:`_on_streaming_entry_shown` replays it."""
        record = self._streaming_record_for(entry)
        if record is not None:
            record.visible = False

    def _flush_streaming_reply(self, record: "_StreamingReply") -> None:
        """Hand the entry the FULL accumulated text and mark it rendered — the
        single place a streamed partial reaches the flow, from either the
        live leg (a delta while visible) or the replay leg (``on_show``).

        ``rendered`` is advanced BEFORE the ``set_item`` on purpose:
        ``set_item`` re-enters flowview (reflow → ``_sync_visibility``), which can
        call straight back into :meth:`_on_streaming_entry_shown`, and a record
        that still looked ``pending`` there would flush a second time."""
        record.rendered = record.text
        record.entry.set_item(replace(record.entry.item, text=record.text))

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

        #3338: EVERY frame — event or display — ends with
        :meth:`_refresh_live_chrome`, so the status line and any OPEN drawer pane
        track the session as a turn runs. The refresh used to live inside the
        display leg only, below a ``continue`` the event branch took, which meant
        the LLM-call events that actually move cost/ctx never refreshed anything.

        #3288 ③c: ``agent_delta`` (:meth:`_handle_agent_delta_event`) coalesces
        streamed reply chunks into ONE flow entry per ``chain_id`` — see that
        method's docstring and :attr:`_streaming_replies`. The entry it
        maintains is finalized by the terminal ``kind="agent"`` DISPLAY frame
        in :meth:`_ingest_frame`, never appended a second time. #3283 ③: that
        coalesce is visibility-gated — an off-screen reply accumulates but does
        not re-render, and replays in one update when it scrolls back.

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

        #3300 (event-ify the intervention-answer echo): ``intervention_answer_submitted``
        (:meth:`_handle_intervention_answer_event`) renders straight to the
        flow, unlike ``user_submitted`` — an intervention answer was never a
        queued inbox item, so there is no sent-queue stage to promote through.

        #3310 N2: ``session_attached`` (:meth:`_handle_session_attached_event`)
        is the switch-reset BARRIER — everything on this stream before it
        belongs to the OLD attached session, everything after to the NEW one
        (N1's no-await critical-section guarantee at the registry seam). On
        receipt, every per-session client state is reset and the NEW session
        is rehydrated from ``history.jsonl`` — v1 is reconnect-shaped, not
        cache-shaped (a cached FlowView would be stale-by-construction: the
        forwarder drops a detached session's frames entirely).
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
                    if etype == "session_attached":
                        try:
                            self._handle_session_attached_event(frame.event)
                        except Exception:
                            logger.exception(
                                "textual chat: session_attached reset+hydrate failed"
                            )
                    elif etype == "user_submitted":
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
                    elif etype == "intervention_answer_submitted":
                        try:
                            self._handle_intervention_answer_event(frame.event)
                        except Exception:
                            logger.exception(
                                "textual chat: intervention_answer_submitted "
                                "ingest failed"
                            )
                    elif etype == "session_halted":
                        try:
                            self._handle_session_halted_event(frame.event)
                        except Exception:
                            logger.exception(
                                "textual chat: session_halted status refresh failed"
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
                else:
                    msg = frame.message
                    if msg.kind == "__end__":
                        break
                    if msg.kind not in _SKIP_KINDS:
                        try:
                            self._ingest_frame(msg)
                        except Exception:
                            logger.exception(
                                "textual chat: frame ingest failed for kind=%r",
                                msg.kind,
                            )
                # F5b + #3338: refresh the live chrome (the always-visible
                # status-values line, plus whichever drawer pane is OPEN) on EVERY
                # frame — DISPLAY **and** EVENT alike. This used to sit inside the
                # DISPLAY leg only, below a ``continue`` the EVENT branch took, so
                # ``llm_called``/``llm_response_received`` — the very frames that
                # move cost and ctx — never refreshed anything: a long tool-loop
                # turn that interleaves no display frame left the numbers stale for
                # its whole duration. Bounded by frame rate (far below a render
                # loop) and guarded so a snapshot read failure never kills the pump.
                try:
                    self._refresh_live_chrome()
                except Exception:
                    logger.exception("textual chat: live chrome refresh failed")
        finally:
            self.exit()

    def _refresh_live_chrome(self) -> None:
        """Re-render everything that must track live session state as frames land:
        the collapsed status-values line, and the drawer pane that is currently
        OPEN (#3338 — before this, a pane was built once at open time and then
        froze, so a Cost/Ctx tab left open showed the figures from the moment it
        was opened).

        Only the OPEN tab is rebuilt, and only on frame arrival. That bound is
        load-bearing, not an optimization: the Ctx pane's ``compaction`` row calls
        ``ctx_compaction_status_fn`` (= ``Session.context_window_status()``, a
        json.dumps + token-estimate of the whole router-view history), which
        ``_snapshot()`` deliberately stores UNCALLED so it never runs per render
        frame. Rebuilding every pane, or rebuilding on a render tick, would
        reinstate exactly the cost that seam exists to avoid.

        One snapshot read feeds both refreshes, so a frame costs one read
        regardless of whether the drawer is open."""
        snap = self._snapshot()
        # #3283 ④: re-cache the keyed per-turn lookup off the SAME snapshot read
        # (see :attr:`_turn_usage_fn`) — the right gutter cannot afford a
        # snapshot per rendered row.
        self._turn_usage_fn = (snap or {}).get("turn_usage_fn")
        self._refresh_status(snap)
        try:
            drawer = self.query_one("#drawer", ContentSwitcher)
        except Exception:
            return  # not yet mounted
        open_tab = drawer.current
        if drawer.display and open_tab:
            self._refresh_pane(open_tab, snap)

    def _refresh_status(self, snap: "dict | None | object" = _UNSET) -> None:
        """Re-render the bottom status-values line from a fresh snapshot (or the
        already-read ``snap``)."""
        try:
            line = self.query_one(StatusLine)
        except Exception:
            return  # not yet mounted
        line.update(self._status_text(snap))

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

        #3299 P1: the Composer is now EXCLUSIVELY for new turns — it no longer
        reads ``pending_intervention_head()`` at all. Answering a pending
        intervention (closed-set select or free-text) happens through the
        :class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
        (:meth:`on_intervention_panel_choice_selected` /
        :meth:`on_intervention_panel_text_submitted`) — its own, never-queued
        transport funnel — for anyone who can reach the panel, plus ONE
        Composer-typed exception: ``/answer`` (#3327). ``/answer`` acts on an
        EXISTING pending intervention, not a new turn, so it is tried FIRST
        through :meth:`~reyn.interfaces.transport.client_transport.ClientTransport.deliver_pending_answer`
        — a direct, un-queued delivery — before falling through to the
        ordinary new-turn path below. This is load-bearing, not cosmetic:
        #3327 found that a Composer submit landing while a turn is blocked on
        an intervention is durably queued (the #3300 sent-queue) but the
        queue only DRAINS once that SAME turn frees — which requires that
        SAME intervention to resolve. A queued ``/answer`` therefore chases
        its own precondition and can never fire; a keyboard-only user who
        ``Esc``-dismissed the panel (#3299 P1's documented escape hatch, which
        returns focus WITHOUT answering) had no way back at all before this
        fix. Every OTHER submission (a fresh turn, or ``/answer`` typed with
        nothing pending) is UNCHANGED: ``deliver_pending_answer`` returns
        ``False`` and ``submit_user_text`` durably queues the line on the
        inbox — visible in the sent-queue region (#3300 P2b, this module) and
        cancelable there (#3300 Y-client, ``↑`` from the composer to focus
        it when nothing is pending, ``Enter`` on a highlighted row to
        cancel) — rather than losing it. Errors are contained and surfaced as
        an error frame the pump renders — a silent input drop is the worst
        failure for a chat box."""
        try:
            if await self._transport.deliver_pending_answer(text):
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

    Full-screen is the default because two inline-driver bugs upstream made
    bounded inline unshippable: on resize the old bounded frame is not cleared
    so stale copies stack (#3285), and the conversation pane collapses to ~1
    line regardless of terminal height (#3286). Both are owned by Textual's
    inline driver, so reyn cannot fix them in inline mode; alt-screen
    sidesteps the driver entirely and both vanish there. #3286 is confirmed
    live-reproduced against reyn's own integration; #3285 is reported upstream
    but reyn's live-TTY integration did NOT reproduce the resize-stacking
    across 4+ resizes in a real terminal
    (https://github.com/tya5/reyn/pull/3291#issuecomment-5081647531) — treat
    #3285-in-``inline`` as not verified-broken here but also not
    verified-clean; re-check live before relying on either claim. The
    scrollback-preservation rationale that originally motivated inline is now
    redundant — alt-screen auto-saves/restores terminal scrollback on
    enter/exit, and Phase 5 restore rebuilds the conversation from
    ``history.jsonl`` on restart. ``inline=True`` remains selectable as an
    escape hatch (``chat.render_mode: inline``) for scrollback-preferring
    users; ``alt-screen`` stays the recommended default regardless. Returns so
    the driver's caller can tear the transport down + print the cost summary.
    """
    app = TextualChatApp(
        transport=transport,
        read_model=read_model,
        agent_name=agent_name,
        config=config,
    )
    await app.run_async(inline=inline)
