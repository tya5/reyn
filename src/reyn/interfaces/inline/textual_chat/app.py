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

Either gutter can be HIDDEN at runtime (#3352, ``ctrl+g`` / ``ctrl+t`` — see
:attr:`TextualChatApp.BINDINGS`), handing its whole column back to the
conversation body. The start state comes from ``chat.gutters.left`` /
``chat.gutters.right``; the keypress is session-scoped and never writes back.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` / ``textual_flowview``
imports never reach an always-loaded module.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import ContentSwitcher, OptionList, Static
from textual_flowview import (
    Anchor,
    Entry,
    EntryState,
    FlowModel,
    FlowView,
)

from reyn.interfaces.inline.textual_chat import palette
from reyn.interfaces.repl._clipboard import (
    copy_to_clipboard,
    copy_to_clipboard_async,
)
from reyn.interfaces.repl._copy_sentinel import COPY_BUFFER_MAX, handle_copy_sentinel
from reyn.interfaces.repl.renderer import (
    _CC_DIM,
    chat_markdown_theme,
    summarize_tool_result,
)
from reyn.interfaces.transport.agui.state import RemoteQueueView
from reyn.interfaces.transport.frames import FrameTag

from ._meta_keys import ELAPSED_SECS_KEY as _ELAPSED_SECS_KEY
from ._meta_keys import EXPANDED_KEY as _EXPANDED_KEY
from ._meta_keys import ORPHANED_RESULT_KIND as _ORPHANED_RESULT_KIND
from ._meta_keys import PIPELINE_RUN_KEY as _PIPELINE_RUN_KEY
from .activity_row import ActivityRow
from .chrome import (
    _MENU_TABS,
    CTX_WARN_PERCENT,
    Composer,
    ConfigWarningLine,
    MenuBar,
    _literal_option_content,
    build_drawer_pane,
    config_warning_text,
    pane_commands,
    pane_needs_literal_rows,
    pane_payload,
    status_line_text,
)
from .compact import compact_caps
from .completion import CompletionPopup, CompletionState, compute_completion
from .gutter import (
    _RUNNING_FRAME_PERIOD,
    RIGHT_GUTTER_WIDTH,
    ReynGutter,
    ReynRightGutter,
)
from .intervention_panel import InterventionPanel
from .loop_probe import LoopTripwire
from .presenter import (
    _RESULT_KIND_KEY,
    _RESULT_META_KEY,
    _RUNNING_SINCE_KEY,
    ReynPresenter,
    _neutralized_label,
)
from .restore import RESUME_DIVIDER, project_restored_frames
from .rewind_picker import RewindPicker
from .search_bar import SearchBar
from .sent_queue import SentQueue

if TYPE_CHECKING:
    from textual.geometry import Offset
    from textual.timer import Timer
    from textual_flowview import VisibilityHandle

    from reyn.core.present.artifact_list import ArtifactRow
    from reyn.interfaces.repl.read_model import ChatReadModel
    from reyn.interfaces.transport.client_transport import ClientTransport
    from reyn.runtime.outbox import OutboxMessage

    from .voice import VoiceInput

logger = logging.getLogger(__name__)

# Display kinds that are control sentinels, not conversation content — skipped
# by the conversation pane. ``__end__`` is handled by the pump loop (it stops
# the app), so it is not here.
#
# #3362 corrected this set AND this comment. It used to also hold
# ``__copy_last_reply__`` / ``__rewind_list__`` under the note "their surfaces
# land in later phases" — a deferral, not a decision, and the later phase never
# came: skipping them made ``/copy`` and ``/rewind`` silent no-ops on the DEFAULT
# TUI (no status line, no clipboard write, no list). Both are now genuinely
# HANDLED in :meth:`TextualChatApp._pump_frames` (clipboard copy + status frame;
# the :class:`~reyn.interfaces.inline.textual_chat.rewind_picker.RewindPicker`
# region + a text fallback) and are gone from this set.
#
# Both former entries are retired (#4534 PR-2 / PR-2b). ``/attach`` and
# ``/session switch`` now go through ``ClientTransport.request_attach`` /
# ``request_session_switch`` — typed operations, not a display-channel
# sentinel — and the AG-UI remote tap's mid-stream switch-follow
# (``transport/agui/endpoint.py``'s ``_SessionFrameSource``) subscribes to
# ``registry.add_attach_listener`` directly instead of consuming a sentinel
# off the outbox. Nothing constructs either kind anymore, so this set is
# currently empty — kept (not deleted) as the documented extension point for
# a future control sentinel that needs skipping here.
_SKIP_KINDS: "frozenset[str]" = frozenset()

# Turn-end event types (#72): when one of these lands on the EVENT-tag frame
# path, any tool row still RUNNING is a confirmed ORPHAN — its completion frame
# can never arrive for THIS turn, since the turn itself just ended. Mirrors the
# plain renderer's ``on_audit_event`` turn-end branch
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

def _configured_gutter_visibility(config) -> "tuple[bool, bool]":
    """``(left, right)`` gutter START visibility from ``chat.gutters`` (#3352).

    Defaults come from :class:`~reyn.config.chat.GutterConfig` (both ``True``)
    rather than being re-typed here, so the config dataclass stays the single
    place the default lives. A missing/partial config (``None``, or a remote
    client with no config object) falls back to those defaults — the same
    ``try``/``AttributeError`` shape ``client_driver._configured_render_mode``
    uses for the sibling ``chat.render_mode`` read."""
    from reyn.config.chat import GutterConfig  # noqa: PLC0415 — TTY-local read

    defaults = GutterConfig()
    if config is None:
        return (defaults.left, defaults.right)
    try:
        gutters = config.chat.gutters
        return (bool(gutters.left), bool(gutters.right))
    except AttributeError:
        return (defaults.left, defaults.right)


def empty_state_hint() -> "object":
    """The conversation pane's empty-state hint (#3476 ②, flowview 0.6.0
    ``empty=``): shown across the viewport while the model has no entries — a
    fresh session previously opened onto a blank void above the composer
    (owner design review). Ambient by design: dim, no colour (the palette
    reserves colour for state), horizontally centered here, vertically placed
    by the FlowView's ``empty_align``. The keys it names are the composer's
    own (``COMPOSER_KEYS``' send / completion rows) phrased for a first
    glance; the Help tab stays the exhaustive source. flowview clears it the
    moment the first entry lands, so there is no app-side show/hide to drift.
    """
    from rich.text import Text

    hint = Text(justify="center")
    hint.append("reyn\n\n", style=f"bold {_CC_DIM}")
    hint.append("Type a message to start\n", style=_CC_DIM)
    hint.append("/ commands · : skills · Help tab for keys", style=_CC_DIM)
    return hint


#: #3476 ④: restored-history page size, in display frames. Hydration appends
#: only the newest page; older frames page in via
#: :meth:`TextualChatApp.on_flow_view_reached_top` as the user scrolls up.
#: 200 frames ≈ a full page-in stays well under one frame budget at the
#: measured ~1µs/entry handle cost, while covering far more than one viewport
#: (so a single page-in absorbs several screens of scrolling before the next).
_HYDRATE_PAGE_FRAMES = 200


def _apply_restored_state(msg: "OutboxMessage", entry: "Entry[OutboxMessage]") -> None:
    """The restored-frame state transition, shared by initial hydration and
    the #3476 ④ lazy page-in — resolved, never RUNNING: mirror the completion
    handler's terminal transition for a settled tool result (SUCCESS unless
    the summary marks a failure); non-tool rows keep DEFAULT."""
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
                EntryState.ERROR if summary.startswith("✗") else EntryState.SUCCESS
            )
    elif msg.kind == "tool_call_completed":
        summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
        entry.set_state(
            EntryState.ERROR if summary.startswith("✗") else EntryState.SUCCESS
        )
    elif msg.kind == "tool_call_failed":
        entry.set_state(EntryState.ERROR)


#: Sentinel for :meth:`TextualChatApp._pane_rows`'s optional ``snap`` argument —
#: distinguishes "no snapshot passed, read a fresh one" from an explicit ``None``
#: snapshot (pre-session), which must NOT trigger a second read.
_UNSET: object = object()

#: #3570 — the minimum wall-clock gap between two repaints of the SAME streamed
#: reply. Deltas arrive at the provider's rate (measured: up to ~1000/s through a
#: proxy that packs many SSE events into one read); repainting at that rate spends
#: the loop re-presenting and re-rendering an O(body) markdown body far more often
#: than a terminal can show. 1/30 s is the knee of the measured curve on the real
#: TUI path (2000 deltas / 60 KB reply, textual-flowview v0.9.0): ``set_item``
#: 1979 → 75 and ``present`` 1908 → 72 with wall-clock 16.1 s → 3.3 s, while going
#: on to 1/20 s bought a further ~5% for 50% more latency. (Those present/wall
#: figures are against the drain's unconditional suspension point already being
#: in place — see ``transport/drain.py``: with it and without this budget, every
#: delta buys its own present, which is what the 1908 is.) It is a REPAINT budget
#: only — the accumulated text is
#: never gated by it (see :class:`_StreamingReply`), and no deferral outlives it
#: (:meth:`TextualChatApp._schedule_streaming_catchup`).
_STREAM_REPAINT_MIN_INTERVAL = 1 / 30


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
      is on screen AND the repaint budget below allowed the last delta through;
      LAGS it while the row is scrolled out of view, or within the budget window.

    ``rendered != text`` is therefore exactly "this row owes the viewport a
    repaint", and :meth:`TextualChatApp._flush_streaming_reply` is the only
    thing that closes the gap — driven by the next delta whose arrival is at
    least :data:`_STREAM_REPAINT_MIN_INTERVAL` after :attr:`last_repaint`, by
    the catch-up timer that bounds that deferral
    (:meth:`TextualChatApp._schedule_streaming_catchup`), or by ``on_show`` when
    the row scrolls back (while it was not visible).

    :attr:`last_repaint` is the app-clock reading of the last such flush — the
    #3570 repaint budget's whole state. It is a RENDER throttle: deltas arriving
    inside the window still land on :attr:`text` in full, they just do not each
    buy their own ``set_item`` (and, through the revision bump, their own
    present + strip render of the whole accumulated body).

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
    last_repaint: float = 0.0
    #: Set when the TURN ended, whatever ended it. The record STAYS in the map
    #: — a terminal completion frame that arrives after the turn-end event still
    #: has to find it to write the authoritative full text. This flag says only
    #: "no further chunks are coming", which is what the #3530 marker reads.
    settled: bool = False

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


class KeyCommitted(Message, namespace="flow_view"):
    """#3624 — posted ONLY when the cursor is committed by keyboard (Enter /
    Space), never by a click. See :meth:`_CursorFlowView.action_activate`.

    flowview >=0.11.0 unified the keyboard highlight and mouse selection into
    one ``current`` cursor: a click now MOVES *and* COMMITS it, so upstream's
    own ``Selected`` fires on a click exactly the same as on Enter/Space, and
    carries no field that tells the two apart (verified against
    ``textual_flowview/_view.py``: ``Selected.__init__`` takes only
    ``flow_view``/``entry``). Reading ``Selected`` as "copy this entry to the
    clipboard" — reyn's pre-0.11.0 intent — would therefore let one stray
    click silently overwrite content the user copied from a DIFFERENT
    application. This message recovers the distinction reyn actually needs at
    the one place upstream itself keeps the two call paths apart: the
    BINDING-driven ``action_activate`` (Enter/Space only — bound in
    ``FlowView.BINDINGS``) versus ``on_click``'s direct call to
    ``self.activate()`` (bypasses the action system entirely, so overriding
    ``action_activate`` cannot see it)."""

    def __init__(self, flow_view: "FlowView[object]", entry: "Entry[object]") -> None:
        self.flow_view = flow_view
        self.entry = entry
        super().__init__()

    @property
    def control(self) -> "FlowView[object]":
        return self.flow_view


class ToggleFoldRequested(Message, namespace="flow_view"):
    """#4697: posted by :meth:`_CursorFlowView.action_toggle_fold` when
    Space is pressed OUTSIDE character-cursor mode (``cursor_visible`` was
    already ``False`` at post time — the app-side handler does not
    re-check it). Distinct from :class:`KeyCommitted` (Enter/Space's copy
    commit, #3624): this is the entry-mode fold/unfold request, #4691 §6's
    owner ruling that highlight movement and open/close are two different
    intents and must not share a trigger."""

    def __init__(self, flow_view: "FlowView[object]", entry: "Entry[object]") -> None:
        self.flow_view = flow_view
        self.entry = entry
        super().__init__()

    @property
    def control(self) -> "FlowView[object]":
        return self.flow_view


class _CursorFlowView(FlowView["OutboxMessage"]):
    """FlowView with the Enter/Space commit split from the click commit
    (#3624) — posts :class:`KeyCommitted` in addition to upstream's
    ``Selected``, so :meth:`TextualChatApp.on_flow_view_key_committed` can
    copy on a KEYBOARD commit without also firing on a click.

    #4697: Space is ALSO rebound (overriding upstream's own
    ``Binding("space", "activate")`` for this subclass — Enter keeps its
    original meaning, only Space moves) to fold/unfold the highlighted
    entry's tool detail instead of committing — #4691 §6's owner ruling
    that decoupled that from highlight movement. See
    :meth:`action_toggle_fold` for the character-cursor-mode guard this
    needed before implementation (measured, issue #4697)."""

    BINDINGS = [
        Binding("space", "toggle_fold", "Fold/unfold tool detail", show=False),
    ]

    def action_activate(self) -> None:
        entry = self.current
        super().action_activate()
        if entry is not None:
            self.post_message(KeyCommitted(self, entry))

    def action_toggle_fold(self) -> None:
        """#4697: Space, outside character-cursor mode, requests a fold
        toggle on the highlighted entry (the app handles the actual
        ``_set_expanded`` flip — see ``on_flow_view_toggle_fold_requested``).
        Inside character-cursor mode, falls through to the ordinary
        Enter/Space activate (copy) path instead, so an in-progress text
        selection is never disrupted by a stray fold.

        ``cursor_visible`` (public property) is used as the guard rather
        than upstream's own private ``_text_active()`` — measured
        equivalent (#4697 issue thread): ``_text_active()`` is
        ``cursor_visible or _tc_anchor is not None``, but every
        ``_set_cursor_visible(False)`` path ALSO clears ``_tc_anchor``,
        so the anchor can never be set while ``cursor_visible`` is
        ``False`` — ``cursor_visible`` alone is a complete public-API
        proxy for it TODAY. Re-verified directly against
        ``_view.py``'s source after #4729's 0.19.0 -> 0.21.1 bump, and
        again after #4792's 0.21.1 -> 0.22.0 bump — both times
        ``_set_cursor_visible`` still unconditionally does
        ``self._tc_anchor = None`` inside its own ``if not visible:``
        branch, unchanged since 0.19.0 — installed pin tracked in
        ``pyproject.toml``, not repeated here as a number that would
        just go stale again on the next bump. This is a DERIVED
        equivalence, not a contract upstream promises: if a future
        flowview version stops clearing the anchor on hide, this guard
        falls out of sync SILENTLY — Space would start folding an entry
        mid text-selection instead of extending it, no exception, no
        warning. Re-verify this docstring's claim on every future
        textual-flowview version bump."""
        if self.cursor_visible:
            self.action_activate()
            return
        entry = self.current
        if entry is not None:
            self.post_message(ToggleFoldRequested(self, entry))


class ScrollableDrawer(ContentSwitcher):
    """The bottom drawer, with keys that can reach a readout taller than it.

    ``ContentSwitcher`` scrolls when its stylesheet says so (see the
    ``#drawer`` rule) but binds no key to do it, so a Help pane of 30 lines in
    a 12-row drawer was reachable by mouse wheel and by nothing else (#3699 —
    measured: 11 of 30 lines on screen, and the 19 missing were the keyboard
    shortcuts the pane exists to list).

    PgUp/PgDn rather than ↑/↓: ``↑`` already means "back to composer" while
    the drawer is open (``chrome.MENUBAR_KEYS``), and rebinding it would trade
    one unreachable thing for another. PgUp/PgDn are unbound in this context
    and already read as "page through content" elsewhere in this app. The
    Help pane lists them from that same ledger, so the pane that was cut off
    now also says how to see the rest.

    A pane that fits is unaffected: with nothing to scroll, these keys move
    nothing rather than being conditionally absent.
    """

    BINDINGS = [
        Binding("pagedown", "scroll_pane_down", "Scroll this pane", show=False),
        Binding("pageup", "scroll_pane_up", "Scroll this pane", show=False),
        Binding("home", "scroll_pane_home", "Top of this pane", show=False),
        Binding("end", "scroll_pane_end", "Bottom of this pane", show=False),
    ]

    def action_scroll_pane_down(self) -> None:
        self.scroll_page_down(animate=False)

    def action_scroll_pane_up(self) -> None:
        self.scroll_page_up(animate=False)

    def action_scroll_pane_home(self) -> None:
        self.scroll_home(animate=False)

    def action_scroll_pane_end(self) -> None:
        self.scroll_end(animate=False)


class TextualChatApp(App):
    """The TTY conversation pane: a FlowView of the live conversation + a
    Composer, both fed/served by one :class:`ClientTransport`.

    The app drains ``transport.frames()`` in a worker, appending each display
    frame to the retained model (event frames are consumed but not yet drawn);
    a Composer submit routes back through the transport. The user's own TURN
    line is NOT echoed locally — it returns as a ``kind="user"`` frame on the
    same stream, so the model is fed entirely from frames and stays equivalent
    to the plain renderer's turn sequence. A COMMAND line is the exception and
    has to be: it emits no ``user_submitted`` audit-event, so nothing would ever
    send that frame back. The shared client-side slash layer writes the echo
    through ``put_display`` (#3595 S5) — still a frame on the same stream, so
    the model is still fed entirely from frames.

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

    Phase 3 adds the bottom-chrome tab-drawer: below the composer, a focusable
    :class:`MenuBar` (which also carries the :class:`StatusLine` status-values
    segment — #3326 collapsed the two into one shared row whenever the
    terminal is wide enough for both, see :meth:`MenuBar._repack`), and a
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
    restart. N1 (#3321) added a ``session_attached``
    ``EventFrame`` the registry puts directly on ``repl_outbox`` at the attach
    seam, with NO ``await`` between the connection-switch flip and the put —
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
    :meth:`_submit` runs ``/answer`` as a COMMAND rather than submitting it as
    a turn — a DIRECT, un-queued delivery, so it can always resolve the
    intervention it targets regardless of turn state. #3595 S5 replaced the
    ``/answer``-only fast path this used to name with the shared client-side
    slash layer, which runs every command that way; (2)
    the Composer's ``↑`` (first line, per :class:`Composer`'s own
    ``_on_key``) now focuses the pending :class:`InterventionPanel` FIRST,
    ahead of the sent-queue, whenever one is showing — the SAME idiom that
    already routes ``↑`` to the sent-queue, extended rather than replaced,
    and registered in :data:`~reyn.interfaces.inline.textual_chat.chrome.COMPOSER_KEYS`
    so the Help pane surfaces it.

    **#3354 — ``/`` and ``:`` completion.** A
    :class:`~reyn.interfaces.inline.textual_chat.completion.CompletionPopup`
    sits directly above the input row (region order: conversation /
    intervention panel / rewind picker / sent-queue / COMPLETION / input) and is
    driven by the
    Composer, which stays focused throughout. :meth:`completion_state` is the
    app's contribution: it resolves the live session + skill list off the
    ``ChatReadModel`` seam and hands them to the pure ``compute_completion``.
    The ``↑``/``↓`` routing above is UNCHANGED while the popup is closed and
    PRE-EMPTED while it is open (the popup's highlight moves instead) — the
    state-dependence is spelled out in both :data:`COMPOSER_KEYS` rows so the
    Help pane teaches it.
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
        # ``show=False``: the Help pane's row for this key is written by hand in
        # ``chrome.MENUBAR_KEYS`` (#3818), because rendering Textual's own
        # identifier here spelled it ``escape`` while every other row said
        # ``esc``. The binding is unchanged — only who describes it.
        Binding("escape", "close_drawer", "Close drawer", show=False),
        # #3352: hide/show either gutter, handing its whole column back to the
        # conversation body. Two bindings, not one, because the upstream
        # granularity is two INDEPENDENT flags (``FlowView.left_gutter_visible``
        # / ``right_gutter_visible``) — reyn follows it rather than inventing a
        # coarser "both" switch. Keys chosen after enumerating every binding
        # reachable from this app: Textual's own ``App``/``Screen`` defaults
        # (ctrl+c, ctrl+q, tab, shift+tab), ``TextArea``'s (the Composer owns
        # focus most of the time — ctrl+k/u/v/w/x/y/z/d/a/e, ctrl+arrows,
        # f6/f7), ``OptionList``'s (the drawer panes), and reyn's own imperative
        # ``Composer``/``MenuBar`` ``_on_key`` keys plus ``SentQueue``/
        # ``InterventionPanel`` ``BINDINGS`` (enter/escape/tab/arrows). ctrl+g
        # and ctrl+t appear in NONE of them, and neither is one of the four
        # keys (↑ ↓ tab esc) the composer-completion popup (#3358) borrows
        # while it is open.
        #
        # A live-binding sweep is NOT sufficient on its own: a key RESERVED by
        # an unimplemented feature exists only in an issue and in deleted code,
        # so it appears in no grep of the current tree. #2193 (open, voice
        # input via Whisper STT — the `voice:` block in ``config/media.py``
        # with nothing reading it) reserves **F2 / Ctrl+R**, inherited from the
        # retired Textual TUI's ``voice_toggle``. ctrl+r is therefore NOT free,
        # and ctrl+r is additionally reverse-history-search in most shells —
        # an expectation users carry into any text-input surface. The retired
        # keymap's OTHER claims (ctrl+g `/find`, ctrl+t rewind-menu edit,
        # ctrl+b/o/w/p/n/l, ctrl+1..7, f3/f4/f7/f9) all belong to features
        # #2193 was explicitly re-scoped AWAY from, so they are dead
        # reservations rather than live ones. ctrl+t's retired use was gated
        # on a rewind MODAL that no longer exists (rewind is a slash command
        # now), and "t" reads as the timing/token column this key hides.
        # ``chrome.RESERVED_KEYS`` is the machine-checkable record of the one
        # reservation that IS still live.
        ("ctrl+g", "toggle_left_gutter", "Show/hide left gutter (state)"),
        ("ctrl+t", "toggle_right_gutter", "Show/hide right gutter (elapsed/tokens)"),
        # #3476 ⑤ / #3692 PR-B ③: in-conversation search (owner-decided entry
        # point, moved off its original `ctrl+f`). flowview 0.13 (#3692
        # PR-A) gave `ctrl+f` its OWN meaning (`cursor_scroll_page_down`,
        # one of a `ctrl+b/d/e/f/u/y` vim-scroll SET) — reyn's search is a
        # measurably DIFFERENT feature (entry-granular substring search
        # over the FULL conversation model, forcing lazily-paged-in older
        # history to materialise first, vs. flowview's row/character-level
        # cursor jump limited to whatever is already materialised, seeded
        # from the current selection/word rather than a typed query), so
        # per the issue body's own decision rule ("different feature ->
        # different key") it moves rather than displacing one vim-scroll
        # key out of its set. `ctrl+/` was the issue's own suggested
        # example but is REJECTED here: it has no single reliable
        # control-byte mapping across terminals (some send 0x1F, some
        # nothing at all), unlike a plain `ctrl+<letter>` — a real risk on
        # the owner's Windows/git-bash environment (#3671), and untestable
        # from here. `ctrl+p` was the first candidate and is a REAL trap:
        # it is free by every enumeration in this file's tradition (no
        # TextArea/flowview/reyn/RESERVED_KEYS claim) yet still fails,
        # because Textual's own `App.COMMAND_PALETTE_BINDING` claims it
        # OUTSIDE the declarative `BINDINGS` list this file's enumerations
        # have always walked (measured: pressing it opened the command
        # palette, not the search bar, in a real ``run_test`` pilot press —
        # not just a BINDINGS-string check, which would have missed this).
        # `ctrl+n` re-verified free against the FULL enumeration —
        # TextArea's own ctrl-bindings (ctrl+a/c/d/e/k/u/v/w/x/y/z,
        # measured off the class directly), flowview's owned set
        # (ctrl+b/d/e/f/u/y), reyn's own existing (ctrl+c/g/o/q/t),
        # ``chrome.RESERVED_KEYS`` (ctrl+r/f2, #2193),
        # ``docs/deep-dives/contributing/cli-redesign.md``'s own proposed
        # binding table (ctrl+c/d/l/r — a DIFFERENT, not-yet-built CLI, but
        # a real reservation worth not colliding with — it ruled out
        # `ctrl+l` too), AND Textual's `App`/`Screen` class attributes
        # beyond `BINDINGS` (`COMMAND_PALETTE_BINDING` — the gap `ctrl+p`
        # fell into) — and PRESSED, not just declared, in
        # ``test_search_bar_3476.py``.
        ("ctrl+n", "open_search", "Search conversation"),
        # #3498: ctrl+c INTERRUPTS the in-flight turn — the terminal-REPL
        # meaning of the key, and what ``ClientTransport.cancel_inflight``'s
        # own docstring already called "the ctrl-c seam" for a seam that had
        # no production caller from any client until now (several runtime
        # docstrings describe cancellation as happening "on Ctrl-C"; they were
        # describing an intent, not a wiring).
        #
        # ``priority=True`` is load-bearing, and the measurement is narrower
        # than it first looks — worth writing down, because a plain binding
        # passes the obvious tests:
        #   * ``TextArea`` binds ``ctrl+c,super+c`` to ``copy``, but that
        #     action is DISABLED while nothing is selected (its
        #     ``check_action``), so with an empty selection the key falls
        #     through and even a NON-priority app binding runs.
        #   * With a live selection in the composer ``check_action("copy")`` is
        #     True, the focused widget consumes the key, and a non-priority app
        #     binding never fires (measured False). A priority binding still
        #     wins (measured True).
        # So the ONLY case that needs priority is "the user has text selected
        # in the composer" — which is precisely the case the owner's
        # unconditional-interrupt decision has to survive, and precisely what
        # ``test_ctrl_c_interrupts_even_with_a_composer_selection`` pins.
        # Behind both of those sits Textual's own ``App`` binding of ``ctrl+c``
        # to ``help_quit`` — the "Press ctrl+q to quit the app" the owner saw
        # instead of a cancel; a subclass binding overrides it (measured).
        #
        # Owner decision: interrupt UNCONDITIONALLY rather than only while a
        # turn runs. With nothing in flight the call is a runtime no-op, and a
        # key whose meaning depends on invisible state is worse than one that
        # always means the same thing. Quitting is unaffected — ``ctrl+q``
        # already owns it, matching the terminal convention (ctrl+c interrupt /
        # ctrl+q quit). The cost is TextArea's ctrl+c copy, which reyn replaces
        # with ``/copy`` and the keyboard cursor's Enter/Space copy (#3476 ⑥).
        Binding("ctrl+c", "cancel_turn", "Interrupt the running turn", priority=True),
        # #3712: return to the newest output from wherever focus is. The
        # conversation pane has its own ``end``/``G``, but those fire only
        # while IT holds focus — i.e. never from the composer, which is where
        # a reader scrolling back actually is. ``priority`` so the focused
        # Input does not swallow it.
        Binding("ctrl+end", "jump_to_latest", "Back to the newest output", priority=True),
        # #3796: a joke — a full-viewport text effect over the conversation
        # pane, toggled by the same key. ``ctrl+l`` because its terminal
        # meaning is "repaint the screen", which is the nearest thing this has
        # to a convention, and because a Textual app repaints continuously so
        # the chord does nothing today. Chosen from an enumeration of every
        # binding this app, the composer and the pane declare, minus the ones
        # that are terminal ALIASES rather than chords (ctrl+i/j/m/h are
        # tab/enter/enter/backspace at the terminal and cannot be taken).
        # ``priority`` so the focused composer does not swallow it.
        Binding("ctrl+l", "toggle_text_effect", "Text effect (joke)", priority=True),
        # #3692 PR-A: the `c` binding that used to gate the text cursor
        # behind an explicit entry step is REMOVED, not rebound — flowview
        # 0.13 made the text cursor always-on (visual mode is the one real
        # mode now); `c` is flowview's OWN key (toggle_cursor), reached by
        # ordinary bubbling since reyn declares no binding of its own for it.
        # #3692 PR-B ①: flowview cannot bind a key for "I don't have focus
        # yet" — only the app can move focus INTO it, so this one addition is
        # reyn's alone to make. `Ctrl+O` was a DEAD reservation from the
        # retired Textual TUI (`chrome.RESERVED_KEYS`'s own comment lists it
        # among the keys #2193's re-scope freed), re-verified free against
        # the same enumeration the ctrl+g/ctrl+t/ctrl+f comments above
        # record. `Shift+Tab` (Textual's own default cycle-focus) still
        # reaches the pane too — this is a direct jump, not a replacement.
        ("ctrl+o", "focus_conversation", "Focus conversation pane"),
        # #4187: voice input (Whisper STT) revival. The retired Textual TUI
        # bound this to Ctrl+R (primary) with F2 as an alias
        # (``chrome.RESERVED_KEYS``' own comment recorded both as claimed by
        # #2193). Only F2 is bound here — Ctrl+R is deliberately NOT reused,
        # per this file's own ctrl+g/ctrl+t enumeration comment above, which
        # already flagged it: "ctrl+r is ... reverse-history-search in most
        # shells — an expectation users carry into any text-input surface".
        # That expectation is exactly what the Composer is (a multi-line
        # TextArea), so claiming ctrl+r here would collide with a
        # convention the composer's own users bring with them, not just with
        # another reyn binding. F2 carries no such prior claim (and was
        # already the retired TUI's alias, not a fresh pick).
        # ``priority`` so the focused composer does not swallow it (same
        # reasoning as ctrl+l's text-effect toggle above).
        Binding("f2", "voice_toggle", "Voice input (dictate)", priority=True),
    ]

    CSS = palette.css("""
    /* #3503: the app paints NO ground of its own — the terminal's background
       shows through. Measured before this: ``#inputrow`` / ``#inputgutter`` /
       ``SentQueue`` / ``MenuBar`` all declare ``transparent`` already, yet all
       painted ``#121212``, because "transparent" means "show what is behind"
       and what was behind is the SCREEN's own ``#121212`` (Textual's dark
       theme). So this could not be fixed per widget — the Screen is the source,
       which is why the fix reaches the whole surface rather than just the two
       regions the report named. Regions that are meant to stand OUT keep
       declaring ``$panel`` explicitly (drawer, completion popup, search bar,
       rewind picker), and the presenter's deliberate ROW TINTS
       (``_CC_USER_BG`` / ``_CC_ERR_BG``) are unaffected — those are content,
       not ground. */
    App { background: @app-background@; }
    Screen { layout: vertical; background: transparent; }
    /* #3542: the drag-selection band. Textual's ansi-dark defaults to
       `ansi_bright_blue`, which the operator found too loud against the
       conversation. Dropping to `ansi_blue` asks the terminal for a different
       one of its sixteen slots — reyn is not overriding the user's colours
       here and never was, it only picks which frame to request. Declared as an
       explicit background/foreground PAIR rather than `text-style: reverse`:
       Textual COMPOSES the selection style onto each cell, so reverse would
       let every coloured run (tool rows, amber intervention headings, dim
       chrome) become its own background and the band would fragment — which
       is a different complaint than "too loud". */
    Screen > .screen--selection {
        background: @selection-bg@;
        color: @selection-fg@;
    }
    FlowView {
        height: 1fr;
        scrollbar-size-vertical: 0;
        margin-bottom: 1;
        /* #4691 Phase 2: NO-OP today, measured, not assumed — kept for what
           breaks if `scrollbar-size-vertical: 0` above is ever removed.
           flowview 0.21.0's CHANGELOG names a real cost: a fold that shrinks
           content below the viewport drops the scrollbar, which widens the
           body — and width is part of the presentation cache key, so
           everything re-presents (upstream's own 50-entry-group figure: 0
           presents reserved vs 12 unreserved). A local repro of that exact
           collapse (1 parent + 50 children + 3 filler rows, 20-row viewport,
           counting presents on collapse) measured FOUR configurations:
           neither declaration = 8 presents; `scrollbar-gutter: stable` alone
           (upstream's own fix) = 4; `scrollbar-size-vertical: 0` ALONE
           (reyn's pre-existing CSS, one line up) = 4 — already the full
           mitigation, because a scrollbar whose SIZE is fixed at 0 never has
           a size to remove, so the body width never moves regardless of
           gutter reservation; adding `scrollbar-gutter: stable` on top of
           `scrollbar-size-vertical: 0` = still 4 — no measurable change.
           Declared anyway, at zero measured cost, because it is the residue
           that survives the day `scrollbar-size-vertical: 0` above is
           removed or changed: without this line, that edit would silently
           reopen the exact re-present storm this comment describes, with no
           test in this repo pinned to catch it (upstream's own mitigation
           lives in ITS test suite, not reyn's, and reyn does not fold/nest
           entries yet — #4691 Phase B — so there is nothing here to exercise
           the interaction today). */
        scrollbar-gutter: stable;
    }
    /* #3496 / flowview#5: ``flowview--highlight`` (``--selected`` was its
       0.11.x synonym for the SAME class; 0.12.0 / #3624 removed the alias, so
       only ``--highlight`` exists now — both names painted the identical row
       either way) is left UNDECLARED on purpose — the addressed row is marked
       in the gutter (see ReynRightGutter's ``is_marked``), never by restyling
       the row. flowview 0.6.1 honours that: an undeclared component class
       paints nothing, because the row overlay uses the *partial* component
       style. Under 0.6.0 it did not (an undeclared class resolved to a
       CONCRETE inherited style and was painted, turning the addressed row
       near-black), which needed a subclass suppressing the accessor; the pin
       bump removed that workaround. ``test_the_addressed_row_keeps_its_own_background``
       is what holds this — it fails if the row's own colours are ever
       disturbed again, whichever side causes it. */
    /* #3490: NO ``flowview--highlight`` / ``--cursor`` component style
       (``--selected`` was the same class's 0.11.x name before #3624 dropped
       the alias). Deliberately left unstyled (flowview's own default) and the
       addressed row is marked in the GUTTER instead — see ReynRightGutter's
       ``is_marked``. A component style cannot do this job: flowview applies it
       via ``Strip.apply_style``, i.e. ``style + segment.style``, so it is only
       ever a BASE under each segment's own attributes and a background here
       vanishes on the rows that carry a full-row ``Presentation.background``
       (the user's own line, failure rows). ``text-style: reverse`` DOES
       survive that merge — it is what #3476 ⑤/⑥ shipped — but inverting
       fg/bg paints a near-white block over the palette (owner review: "白背景
       になって気持ち悪い / 洗練されてたデザインを壊してる"), so surviving the
       merge is necessary and not sufficient: the mark has to be CONTENT. */
    #inputrow {
        height: auto;
        max-height: 8;
        border-top: solid @rule@;
        border-bottom: solid @rule@;
    }
    #inputgutter {
        width: 2;
        height: auto;
        color: @quiet@;
    }
    Composer {
        height: 3;
        max-height: 6;
        border: none;
        padding: 0;
        /* #3503: ``TextArea``'s own DEFAULT_CSS sets ``background: $surface``
           (measured: it painted ``#1e1e1e`` while everything around it was
           transparent), so the input box needs its own opt-out — a transparent
           Screen alone does not reach it. */
        background: transparent;
    }
    /* The placeholder must read as an invitation, not as typed text. Textual's
       own rule is ``color: $text 40%``, which under ``ansi-dark`` resolves
       ``$text`` to the ansi_default MARKER — and alpha compositing DROPS the
       marker, so the 40% never applies and it painted at the terminal's full
       default foreground (measured: ``Color('default')``, no dim). ``dim``
       is used instead of a muted colour because it leaves the HUE to the
       terminal, which is what a themed default is for; ``$text-muted`` hits
       the identical trap, and a concrete grey would pin a colour the user's
       theme is entitled to choose. */
    Composer > .text-area--placeholder {
        text-style: dim;
    }
    /* #4542: text-style, not color — same "$text-muted is inert under the
       ansi-* themes" reasoning MenuBar's own comment below already states
       (#3522/#3528/#3505's ansi-dark measurement). @telemetry@ (dim) is a
       DEDICATED token (palette.py), separate from @recede@ despite sharing
       today's underlying value — see that token's own docstring. */
    StatusLine {
        height: 1;
        text-style: @telemetry@;
        padding: 0 1;
        /* #4542: pins the text to the row's RIGHT edge without touching
           width — in the ``.-shared`` (merged) case the box is already
           auto-tight to its own text, so this is a no-op there; in the
           own-row fallback below, width stays 100% (load-bearing — see
           that rule's own comment on why an auto-width own-row status
           line can overflow), so text-align is what achieves "pinned
           right" there instead of a spacer, which would need width: auto
           to have anything to push against and would reopen the overflow
           this rule protects against. */
        text-align: right;
    }
    /* #4194: bold, not a colour — palette.py's own rule (@attention@ is the
       ONLY semantic colour this CUI claims, for the intervention panel;
       everything else that must stand out uses an attribute so the hue
       stays the terminal theme's to choose). ConfigWarningLine's own
       DEFAULT_CSS sets height/text-style/padding; nothing else to add
       here — unlike MenuBar/StatusLine, this widget owns its full sheet
       because it is a plain top-level sibling, not something MenuBar
       repositions. */
    /* #3326: when StatusLine SHARES a row with Tab widgets (MenuBar._repack's
       merge case), width: auto keeps it sized to its own text instead of
       stretching to consume the row's remaining space (which would push the
       tabs before it out of a natural left-packed layout). This is scoped to
       the ``-shared`` class ONLY — when StatusLine has a row to itself (no
       room to merge), it keeps the base rule's width (100% of its row),
       which is load-bearing: an unconstrained auto-width single-line Static
       renders at its full natural width regardless of the terminal, so an
       over-long status string (e.g. a long model id) would overflow off the
       right edge instead of being contained/clipped by its row. */
    StatusLine.-shared {
        width: auto;
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
        /* #3528: ``text-style``, not ``color``. The pair this replaces was
           ``color: $text-muted`` here and ``color: $text`` on
           ``:focus-within`` — a brightness step that has been INERT since
           #3505 adopted ``ansi-dark``, where both variables resolve to the
           same ``ansi_default`` marker. Measured: moving focus from the
           composer into the menu changed exactly ONE cell on the whole
           screen, and it was the composer's own text cursor vanishing — a
           cue you only notice by its absence. ``dim`` survives that theme
           (it is an SGR attribute, not a colour) and is the mechanism the
           tabs' own active/inactive distinction already relies on. */
        text-style: dim;
        padding: 0 1;
    }
    MenuBar:focus-within { text-style: none; }
    /* The "you are here" marker. Un-dimming alone is too quiet to answer the
       owner's report, and it reaches only the ACTIVE tab in practice (the
       inactive ones are dim already, so the bar as a whole does not visibly
       lift). ``reverse`` inverts using the TERMINAL's own two colours, so it
       needs no palette entry and works on a light or dark theme alike — the
       standard menu-bar idiom, and here it is confined to one short label.
       #3490 rejected ``reverse`` for the addressed CONVERSATION row, which is
       not this case: there it inverted a full-width row of prose into a
       near-white block; a single tab is the size the idiom is built for. */
    MenuBar:focus-within Tab.-active { text-style: reverse bold; }
    MenuBar Tab { padding: 0 1; }
    /* #4542 (owner ruling, REVERSES #3326's own "tone down" rule below —
       kept as history, not repaired quietly): the redesign's own words are
       "選択中の項目のみ...強調表示を行う" (ONLY the selected item gets
       emphasis) — the opposite instruction from #3326's "match it to the
       muted status line so it doesn't jump out". #3326 solved a real
       problem (Tab's own DEFAULT_CSS ``.-active`` at full-brightness
       ``$foreground`` against every other tab's 50%-muted one read as loud)
       by TONING DOWN toward Telemetry's own tone — but #4542 gives
       Telemetry its OWN dedicated, permanently-dim style (``@telemetry@``,
       see StatusLine's rule above and palette.py), so "match the active tab
       to it" now means "make the active tab look like the OBSERVATION half
       of the row" — backwards from what emphasis should read as. No
       ``color`` override here anymore: the active tab keeps the terminal's
       own normal foreground (never toned down), ``bold`` alone is the
       emphasis. */
    MenuBar Tab.-active {
        text-style: bold;
    }
    /* No separator rule between the menu row and its drawer — they read as one
       continuous, edge-to-edge block (the $panel background is the only cue). */
    #drawer {
        height: auto;
        max-height: 12;
        /* #3699: the readout panes (Help/Cost/Ctx) are routinely taller than
           this cap — Help alone is 30 non-blank lines — and without this the
           remainder was CLIPPED: no scrollbar, no indication, nothing any key
           could reach. Measured before the fix: 11 of Help's 30 lines on
           screen, and the 19 missing ones were the keyboard shortcuts, i.e.
           the reason the pane is opened at all.
           The overflow belongs HERE rather than on the pane: a ``Static`` is
           not a scroll container, so capping the Static instead truncates its
           virtual size to the cap (measured: virtual height 12 for 30 lines of
           content) and there is then nothing left to scroll to. The list panes
           were never affected — ``OptionList`` scrolls itself. */
        overflow-y: auto;
        background: @surface@;
        padding: 0;
    }
    /* OptionList ships an all-round default border — strip it so the drawer
       content is edge-to-edge (full-width highlight rows, no side frame). */
    #drawer OptionList {
        height: auto;
        max-height: 12;
        background: @surface@;
        border: none;
        padding: 0;
    }
    /* #3699: deliberately NO max-height here — the pane must be allowed to be
       its full content height so the scroll container above has something to
       scroll to. Capping the Static instead clamps its virtual size and the
       content past the cap stops existing rather than moving off screen.
       Horizontal padding unrelated to this rule's own concern (height) —
       touched by #4554, see that rule change's own comment just below for
       why 0 became 2. */
    /* #4554: the tab label sits 2 cells in from the drawer's left edge
       (MenuBar's own `padding: 0 1` + Tab's own `padding: 0 1`, above), but
       a Static pane's content started at column 0 — no shared left edge
       between a tab and the pane it opens, most visible on the Cost tab's
       column-aligned table (reyn-reviewer, #4544's own investigation).
       Fixes ALL 5 Static panes (cost/ctx/pipe/cron/help) from this ONE
       rule — `_MENU_TABS` (chrome.py) has 14 entries, `_LIST_PANES` has 9;
       the other 9 are OptionList, explicitly excluded (architect's own
       measurement, #4554): OptionList already has `padding: 0` above by
       design (full-width row highlight on selection — adding padding here
       would shrink that highlight), so this rule intentionally targets
       Static only, never OptionList. */
    #drawer Static { height: auto; padding: 1 2; }
    """)

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
        "_call_parents",
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
        # presentation. #3318: the default presenter reads chat.neutralize_body
        # off the injected config (opt-in body ESC/OSC neutralize) — an
        # explicitly-injected `presenter` (tests, or a future caller) owns its
        # own flag instead, same as it already owns its own clock.
        self._presenter = presenter or ReynPresenter(
            clock=self._clock,
            neutralize_body=bool(
                getattr(getattr(config, "chat", None), "neutralize_body", False)
            ),
        )
        # Running tool-call entries keyed by op_id (== the dispatcher's
        # deterministic args_hash, meta["op_id"]) so a later completion/failure
        # frame transitions the SAME entry RUNNING → SUCCESS/ERROR (CC parity).
        self._running_tools: "dict[object, Entry[OutboxMessage]]" = {}
        # #4691 Phase B B1: the litellm-call TREE PARENT for a given call_id
        # (#4691 Phase 1 ①②, #4734) — every ``kind="agent"`` row carrying a
        # ``call_id`` registers itself here on arrival (#4777: unconditional,
        # NOT gated on a provider's own ``finish_reason`` string — see the
        # registration site's own comment, ``_ingest_frame``), and every
        # ``tool_call_started``/``completed``/``failed``
        # frame carrying a matching ``meta["call_id"]`` looks itself up here
        # to find which Entry to nest under (``parent.append_child(...)``
        # instead of the flat ``self.conversation.append(...)``). KEYED, not
        # ORDER-based (owner ruling B, #4691, via #4734's review) — a dict
        # lookup by call_id can never attach a tool row to the wrong parent
        # even if dispatch order or interleaving assumptions ever break,
        # unlike a single "most recently seen" pointer (the design #4734's
        # review rejected for exactly this reason). Entries are never
        # removed — a call_id is a member of exactly one turn's history and
        # is never reused, so the dict only grows for the life of the
        # conversation (bounded by the same session lifetime the flow model
        # itself already is).
        self._call_parents: "dict[str, Entry[OutboxMessage]]" = {}
        # #4691 arc item ① (final item): the CURRENT turn's own ``kind="user"``
        # row — set the moment :meth:`_handle_turn_started_event` promotes it,
        # cleared at that same turn's end (the ``_TURN_END_EVENT_TYPES`` leg of
        # :meth:`_pump_frames`). ``_ingest_frame``'s fallback append (no
        # ``call_id`` parent found) nests under THIS entry when it is set,
        # instead of appending flat — the turn boundary is the same one
        # ``_handle_turn_started_event``/``_TURN_END_EVENT_TYPES`` already use
        # for the sent-queue promotion and the orphan sweeps (architect's own
        # finding: "no new surface needed — turn boundaries already exist on
        # both sides, already consumed elsewhere"), so this reuses it rather
        # than inventing a third.
        #
        # A single ``Entry | None`` field, NOT a dict — deliberately absent
        # from :attr:`_PER_SESSION_DICT_STATE`, whose uniform ``.clear()`` loop
        # only knows how to empty a dict/list. #4776 was the SAME omission
        # once already (a per-session dict forgotten from that tuple); the
        # shape here is different (not dict-valued at all, so it could never
        # have joined that tuple even by inclusion) but the FAILURE MODE is
        # the same one — a per-session field with no explicit reset — so the
        # reset is spelled out explicitly at the session-switch site
        # (:meth:`_handle_session_attached_event`) instead.
        self._current_turn_parent: "Entry[OutboxMessage] | None" = None
        # #4380/#4429 originally had a lifecycle-marker bundling tracker
        # here — removed (2026-08-13): no reachable trigger ever produces
        # two adjacent occurrences (see ``_ingest_frame``'s own docstring
        # for the measurement). If a real screen ever DOES show several
        # denials in a row, that screen is the evidence to redesign
        # against.
        #: #4187: voice input. ``None`` until F2 is first pressed — lazy so an
        #: install without the ``reyn[voice]`` extra never pays anything for a
        #: key nobody used (mirrors ``text_effect``'s import-on-press shape).
        #: The type is import-guarded under ``TYPE_CHECKING`` only — ``.voice``
        #: itself is imported lazily, inside :meth:`action_voice_toggle`, same
        #: as ``text_effect`` already is; this annotation costs nothing at
        #: runtime.
        self._voice_input: "VoiceInput | None" = None
        #: True only while a captured recording is being transcribed — guards
        #: a second F2 press from re-entering ``stop_recording()`` mid-flight.
        self._voice_busy: bool = False
        #: Armed while a recording is open, to enforce ``voice.max_duration_s``
        #: — ``None`` whenever nothing is recording (never left dangling: every
        #: path that ends a recording before it would fire disarms it first,
        #: see :meth:`_voice_cancel_timeout_timer`).
        self._voice_timeout_timer: "Timer | None" = None
        #: Watches how late the event loop runs (#3539). Always on; see
        #: ``loop_probe`` for why it is not opt-in.
        self._loop_tripwire = LoopTripwire()
        #: #4761 ②: the App's own message-pump heartbeat — incremented by
        #: :meth:`on_timer` ONLY for :attr:`_pump_heartbeat_timer`'s own
        #: ticks, which (no ``callback=`` given to ``set_interval``) post an
        #: ``events.Timer`` message that only advances this counter once
        #: Textual's own message-processing loop for THIS App actually
        #: dequeues and dispatches it — unlike ``self._loop_tripwire``'s
        #: worker, which is an independent ``asyncio`` task and keeps running
        #: even if the App's own pump is the thing that's stuck. Silent by
        #: construction (nothing reads this except the two log lines below,
        #: which were already once-per-episode before this existed) — see
        #: :attr:`_pump_heartbeat_timer`'s own start site for why an
        #: unboundedly-running counter needs no separate bound of its own.
        self._pump_ticks = 0
        self._pump_heartbeat_timer: "Timer | None" = None
        #: #4761 ③: total Key events this App has ever received, counted in
        #: :meth:`on_event` — the earliest, focus-independent seam ②'s own
        #: docstring already established. Distinguishes H3 (input never
        #: reaching the App at all) from a live pump that simply has
        #: nothing to do: ② alone can show the pump is still ticking during
        #: a stall, but a ticking pump with zero keys arriving despite an
        #: operator who reports pressing several is H3, not H1/H2. Same
        #: rolling-window treatment as ②'s ``pump_ticks`` (see
        #: ``_watch_loop_responsiveness``) — a raw cumulative total alone
        #: cannot say whether it moved DURING the stall being reported.
        self._keys_received = 0
        #: The last ``events.Key`` OBJECT counted, by identity, not value —
        #: measured directly (not assumed): Textual dispatches the SAME
        #: Key event object to this App's own ``on_event`` twice for some
        #: keys (``escape``, confirmed by ``id()``; an ordinary printable
        #: key consumed by a focused Input along the way did not repeat).
        #: Without this guard the counter would silently over-count
        #: exactly the keys most likely to matter for a stall report
        #: (Esc, an attempt to break out) — dedup by ``is``, not equality,
        #: since two SEPARATE real presses of the same key are two
        #: distinct objects and must both count.
        self._last_counted_key_event: "object | None" = None
        #: One row per pipeline RUN, keyed by ``run_id`` — every step frame for
        #: that run folds into it (:meth:`_coalesce_pipeline_step`).
        self._pipeline_runs: "dict[str, Entry[OutboxMessage]]" = {}
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
        # #4574 design B: the "artifacts" tab's own rows, cached alongside
        # ``_pane_commands`` at the SAME refresh (``_refresh_pane``) — a
        # pure-inline row (`ref is None`) carries no slash command at all
        # (there is no OS ref to `/open`), so `on_option_list_option_selected`
        # needs the row OBJECT itself (for `inline_content`/`media_type`) to
        # materialize+open it, not just the command-string list every other
        # pane action reads.
        self._artifact_rows_cache: "list[ArtifactRow]" = []
        # #4494 design C: which source the CURRENT cache above came from —
        # "live" (the conversation-derived list, the default) or
        # "ref_table_fallback" (the durable artifact-ref table, consulted
        # only once the live list comes back empty — see
        # :meth:`_maybe_refresh_remote_artifact_fallback`). Threaded into
        # ``chrome.pane_payload``/``pane_commands`` so the disclosure row
        # is appended (or not) alongside the right rows/commands.
        self._artifact_rows_source: str = "live"
        # #4601: the fallback's pre-cap total (M in "newest N of M") —
        # only meaningful when ``_artifact_rows_source ==
        # "ref_table_fallback"``; 0 otherwise (the disclosure text is
        # never rendered for the "live" source, so the stale value is
        # never read in that state).
        self._artifact_rows_fallback_total: int = 0
        # #3288 ③c: in-flight streamed reply, keyed by ``chain_id`` — the SAME
        # authoritative correlation id ``RouterLoop._emit_agent_delta`` stamps
        # on every ``agent_delta`` audit-event AND the one the terminal
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
        self._streaming_replies: "dict[tuple[str, object], _StreamingReply]" = {}
        # #3570: the one-shot timer that BOUNDS a repaint deferral, or ``None``
        # when nothing is deferred. Not per-session state (it holds no session
        # identity and its callback iterates whatever is in-flight at the time),
        # so it is deliberately NOT in :attr:`_PER_SESSION_DICT_STATE` — a switch
        # clears the records and the timer then finds nothing to flush.
        self._streaming_catchup: "Timer | None" = None
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
        # #3352: each gutter's START visibility, read from
        # ``chat.gutters.left`` / ``chat.gutters.right`` and applied to the
        # FlowView in :meth:`compose`. Runtime toggles
        # (:meth:`action_toggle_left_gutter` / :meth:`action_toggle_right_gutter`)
        # go straight to the widget and are NOT mirrored back here — the
        # FlowView's own ``left_gutter_visible`` / ``right_gutter_visible`` is
        # the single source of truth for the live state, so there is no second
        # copy to drift.
        self._gutter_start = _configured_gutter_visibility(config)
        # #3362: the newest-first ring of agent reply TEXTS that ``/copy [N]``
        # targets — the same ring, sized by the same shared
        # :data:`~reyn.interfaces.repl._copy_sentinel.COPY_BUFFER_MAX`, that the
        # plain client keeps in ``run_output_loop``. Fed from BOTH sources a
        # reply can reach this pane through: live ``kind="agent"`` frames
        # (:meth:`_ingest_frame`, including a streamed reply's authoritative
        # completion) and the durable ``history.jsonl`` projection
        # (:meth:`_hydrate_from_history`) — so a reply the user can SEE after a
        # restart or a session switch is a reply ``/copy`` can reach, rather
        # than the pane and the command disagreeing about what exists. Not a
        # dict, so it is reset explicitly on a session switch rather than by
        # :attr:`_PER_SESSION_DICT_STATE`'s uniform ``.clear()`` loop.
        self._recent_replies: "deque[str]" = deque(maxlen=COPY_BUFFER_MAX)
        # #3476 ④: restored frames older than the hydrated tail page, oldest
        # first — consumed from the end, one page per ReachedTop, by
        # :meth:`on_flow_view_reached_top`. Reset by every
        # :meth:`_hydrate_from_history` call (initial mount AND session
        # switch), so a switch can never page in the previous session's
        # leftovers.
        self._older_frames: "list[OutboxMessage]" = []
        # #4387 Phase B ② (remaining consumers): the running total of frames
        # already accounted for (materialised into ``self.conversation`` PLUS
        # whatever remains in ``self._older_frames``) as of the last
        # ``project_restored_frames`` call over the currently loaded message
        # log. :meth:`_extend_older_frames_from_disk` uses this to cut a
        # freshly re-projected (now possibly longer) frame list at the right
        # boundary — projection is not 1:1 with messages (some project to
        # nothing, tool-call correlation looks back a message), so this must
        # be a frame-count delta, never a raw-message-count one. Reset
        # alongside ``self._older_frames`` on every hydrate/switch.
        self._history_frame_count = 0
        # #3490: the entries the addressed-row rail is currently painted on.
        # flowview's Highlighted/Selected report only the entry MOVED TO, so the
        # one moved AWAY from is tracked here — it needs its gutter re-derived
        # too, otherwise the rail is left behind on it.
        self._marked_cursor: "Entry[OutboxMessage] | None" = None

    @property
    def cursor_position(self) -> "Offset":
        """Where the terminal pen is left at the end of every frame (#3621).

        Textual returns the pen here after each render, so this is also the point
        an IME anchors its candidate window to. Upstream keeps it as a STORED
        value that only ``TextArea`` refreshes — on focus, and on cursor moves.
        The value it stores is ``cursor_screen_offset``, which is derived from
        the widget's ``content_region``, so it goes stale as soon as the composer
        is laid out somewhere else while the cursor itself sits still. The
        conversation growing does exactly that, and it happens on repaints rather
        than on keystrokes.

        A stale offset does not merely lag — it names whatever now occupies those
        rows. Measured on a 30-row terminal: the stored value stayed two rows
        below the real cursor, i.e. ON THE MENU BAR, and the IME window followed
        it there. Frames after a keystroke were right; frames after a repaint
        were wrong. That is precisely the owner's report of a candidate window
        jumping without any typing.

        Deriving it on READ removes the staleness rather than chasing it: there
        is no layout event to subscribe to that covers being MOVED (``Resize``
        fires on size, not position), so any push-based refresh would have to
        find every mover. The composer knows where its own cursor is, so this
        asks it at the moment the answer is used.

        The stored value still backs every other case — no focus, or a focused
        widget that is not the composer — so nothing that relied on it changes.
        """
        composer = self.screen.focused if self.is_attached else None
        if isinstance(composer, Composer):
            try:
                return composer.cursor_screen_offset
            except Exception:  # pragma: no cover - geometry not ready yet
                pass
        return self._cursor_position

    @cursor_position.setter
    def cursor_position(self, value: "Offset") -> None:
        # Upstream writes here from TextArea's own events. Kept as the fallback
        # for every widget that is not the composer.
        self._cursor_position = value

    def compose(self) -> ComposeResult:
        # #3671: reyn builds the ENTIRE widget tree in this generator —
        # see startup_timing.py's mark_app_constructed docstring for why
        # this is inside tui-boot's own breakdown, not Textual's boot.
        from reyn.runtime.startup_timing import stage  # noqa: PLC0415
        with stage("tui-boot:compose"):
            # Held so the frame pump can start/stop the per-entry BODY animation
            # (``animate_entry``/``stop_entry_animation``) that drives a RUNNING tool
            # row's live spinner + elapsed (Phase ②).
            self._flow: "FlowView[OutboxMessage]" = _CursorFlowView(
                model=self.conversation,
                presenter=self._presenter,
                decorator=ReynGutter(
                    frame_period=_RUNNING_FRAME_PERIOD,
                    # The app's own injectable clock, as the right gutter already
                    # takes — production passes ``time.monotonic``, so this is the
                    # same behaviour, and it lets a test drive the blink instead of
                    # sleeping through a real frame period.
                    clock=self._clock,
                    # #3530: blink a reply that is still receiving chunks. Read
                    # live off ``_streaming_replies`` each repaint — the same
                    # record the terminal completion frame pops — so the marker
                    # can never disagree with whether the stream is actually
                    # still open.
                    is_streaming=self._is_streaming_entry,
                ),
                gutter_width=_GUTTER_WIDTH,
                # Phase ④ (#3283): the RIGHT gutter shows per-entry elapsed time
                # (tool rows) AND the row's turn's real prompt/completion token
                # split (agent reply rows, via the keyed per-turn lookup) — see
                # ReynRightGutter and its two halves for the content-set decisions.
                # additive flowview params; the LEFT gutter/state contract above is
                # untouched.
                right_decorator=ReynRightGutter(
                    clock=self._clock,
                    usage_lookup=self._turn_usage,
                    # #3490, moved to this side by #3526 (owner directive): the
                    # addressed-row rail. Read live off the view each repaint (not
                    # pushed in on every move) so the gutter can never hold a stale
                    # copy of which entry is current.
                    is_marked=self._is_addressed_entry,
                ),
                right_gutter_width=RIGHT_GUTTER_WIDTH,
                spacing=1,
                anchor=Anchor.STICKY_BOTTOM,
                # Native running-blink: FlowView owns the animation clock and
                # re-invokes the time-based ReynGutter each tick (no app-side timer).
                animation_fps=self.ANIMATION_FPS,
                # #3476 ②: a fresh session previously opened onto a blank void
                # above the composer (owner design review). flowview 0.6.0's
                # empty state shows this hint across the viewport while the model
                # has no entries and clears itself the moment the first entry
                # lands — no app-side show/hide wiring to drift.
                empty=empty_state_hint(),
                empty_align="middle",
                # #3476 ④: fire ReachedTop while the top edge is still a few rows
                # away, so the next history page is in place by the time the user
                # actually arrives at it.
                reach_threshold=3,
                # #3476 ⑥: the keyboard cursor (the visual affordance #3470
                # deferred to this PR). Reached the SAME way #3470 already
                # established — Shift+Tab focus-cycling into FlowView, Esc back
                # out (#3399 gate) — never a new focus path. flowview owns
                # ↑/↓/PageUp/PageDown/Home/End moving the cursor once FlowView
                # has focus; while it doesn't, those keys are unaffected (the
                # composer's own PageUp/PageDown delegation, #3470, calls
                # actions on ``self._flow`` directly and never depends on this
                # flag).
                # #3624: flowview 0.11.0 unified this with mouse-driven selection
                # (a click now moves+commits the SAME cursor) — ``selectable=``
                # is the current name (``highlight=`` was removed in 0.12.0).
                # See ``_CursorFlowView``/``KeyCommitted`` above for how
                # reyn keeps the copy-on-Enter/Space intent without also copying
                # on a click.
                selectable=True,
                # #3507 / flowview 0.8.0 (#7), #3692 PR-A (flowview 0.13's yank,
                # renamed from copy_yank): the text cursor's yank writes through
                # THIS sink instead of the default OSC 52. reyn already owns a local
                # clipboard path (``pbcopy``/``xclip``/``wl-copy``/``xsel``) that
                # works on macOS Terminal and through tmux, where OSC 52 silently
                # does not — and unlike OSC 52 its result is observable, so a failed
                # yank can be reported instead of looking like it worked. Upstream
                # added this per-view seam for exactly this case; the alternative
                # (overriding ``App.copy_to_clipboard``) caught every
                # Textual-originated copy in the app to fix one widget's.
                clipboard=self._write_clipboard,
                # flowview 0.17.0 / #4171: without this, `*`/`n`/`N` search reads
                # rows through the render path, so an entry that has never
                # scrolled into view has no Presentation yet and search sees the
                # "Loading..." placeholder instead of its content — silently
                # missing matches above the rendered window. This hands flowview
                # the raw message text so it can search the whole model directly,
                # not just what's been rendered. Deliberately `msg.text`, not
                # what the gutter decorates onto it — the gutter prefix is
                # decoration, not message content, and a match there would be a
                # confusing result to land a search cursor on.
                search_text=lambda msg: msg.text,
            )
            # #3352: apply the configured START state. flowview has no constructor
            # parameter for gutter visibility (both flags initialise True), so the
            # only way to open with one hidden is to set it here — before mount,
            # where ``set_gutter_visible`` short-circuits its relayout and geometry
            # syncs on its own at mount time. No flash: nothing has painted yet.
            left_start, right_start = self._gutter_start
            self._flow.set_gutter_visible("left", left_start)
            self._flow.set_gutter_visible("right", right_start)
            yield self._flow
            # #3299 P1: the grouped intervention panel sits BETWEEN the flow and
            # the input row (region order shared with the sibling #3300 queue arc,
            # plus #3362's rewind picker: conversation / intervention panel /
            # rewind picker / sent-queue / input).
            # Collapsed by default (``display=False`` — see
            # ``InterventionPanel.on_mount``); shown + auto-focused only while an
            # intervention is pending (:meth:`_present_intervention`).
            self._iv_panel = InterventionPanel(id="intervention-panel")
            yield self._iv_panel
            # #3362: the /rewind checkpoint picker sits between the intervention
            # panel and the sent-queue — same collapsed-by-default region shape
            # (``display=False`` in its ``on_mount``), shown only while a bare
            # ``/rewind`` is offering checkpoints.
            self._rewind_picker = RewindPicker(id="rewind-picker")
            yield self._rewind_picker
            # #3300 P2b: the sent-queue region sits BETWEEN the intervention
            # panel and the input row (region order: conversation / intervention
            # panel / rewind picker / sent-queue / input — the intervention/queue/
            # input ordering was pinned by the architect design pass so the sibling
            # #3299/#3300 P1 coders never collide on this zone; #3362 added the
            # rewind picker above the queue, inside the same zone).
            # Collapsed by default (``display=False`` — see ``SentQueue.on_mount``);
            # shown while at least one message is queued, undispatched.
            # #3693: the live-turn line sits between the rewind picker and the
            # queue, so the zone reads past (conversation) -> now (this) -> next
            # (queue) -> the line being typed. Non-focusable, so Tab/Esc still walk
            # the same path to the composer they did before it existed.
            #: #3777: how many entries THIS TURN has produced, counted as they
            #: arrive rather than derived from two reads of the model. The baseline
            #: is the turn's start, which the reader saw; the previous counter's
            #: baseline was the moment the reader scrolled away, which they did
            #: not, so its number could never be interpreted. Reset by
            #: :meth:`_reset_turn_entries` at turn start and nowhere else — in
            #: particular NOT on a scroll edge.
            self._turn_entries = 0
            self._activity = ActivityRow(id="activity-row", clock=self._clock)
            yield self._activity
            self._sent_queue = SentQueue(id="sent-queue")
            yield self._sent_queue
            # #3354: the / and : completion popup sits DIRECTLY above the input row
            # (the last region before the composer), so the candidate list grows
            # upward out of the line being typed — the same direction and adjacency
            # the retired inline app's completion menu had. Collapsed by default
            # (``display=False`` — see ``CompletionPopup.on_mount``).
            self._completion = CompletionPopup(id="completion")
            yield self._completion
            # #3476 ⑤ (ctrl+n since #3692 PR-B ③): the search bar — the last
            # chrome region before the composer (collapsed by default; the
            # completion popup above it can never be open at the same time,
            # since completion follows COMPOSER typing and the search bar owns
            # focus while visible).
            self._search_bar = SearchBar(id="search-bar")
            yield self._search_bar
            with Horizontal(id="inputrow"):
                yield Static("❯", id="inputgutter")
                yield Composer(
                    # ``<key> to <verb> · <key> to <verb>`` (#3801), the shape
                    # ``rewind_picker`` already used. The previous form separated
                    # the two clauses with a comma and gave the second one a
                    # different grammar ("for a newline"), so the two halves of one
                    # hint read as two kinds of statement.
                    # "add a line", not "break the line": measured on a real
                    # terminal, the longer wording truncated at 65 columns where
                    # the pre-#3801 text fit at 60. This one costs one column over
                    # the old text rather than five. ("to newline" is shorter still
                    # and was rejected — it reads as a typo, and a hint nobody
                    # trusts is worse than a hint that wraps.)
                    placeholder=(
                        "Type a message — enter to send · shift+enter to add a line…"
                    )
                )
            # #4194: the config-warning indicator, mounted BEFORE MenuBar so it
            # sits above the menu row in compose order (measured, headless
            # ``App.run_test``: this ordering plus `1fr` FlowView is what leaves
            # the always-visible last row — MenuBar/StatusLine — untouched,
            # exactly where #2280's halt banner already depends on it staying).
            # Conditionally yielded, not yielded-then-hidden: `unknown_config_key_count`
            # is fixed for the whole session (reyn.yaml needs a restart to
            # change), so there is no later render tick that would need to grow
            # this row in — an operator on a clean config never pays even the
            # empty-row layout cost the #4194 measurement confirmed a present
            # row would take.
            # #4357: `keys=` passes the actual `{key: hint}` dict so the line
            # names the offending keys (and destinations, where known)
            # instead of only a bare count.
            config_warning = config_warning_text(
                getattr(self._config, "unknown_config_key_count", 0),
                keys=getattr(self._config, "unknown_config_keys", None),
            )
            if config_warning is not None:
                yield ConfigWarningLine(config_warning, id="config-warning")
            # Bottom chrome: a focusable menu row that also carries the slim
            # status-values segment (#3326: MenuBar owns placing StatusLine on
            # whichever row has room, collapsing the two previously-separate rows
            # into one whenever the terminal is wide enough), then a drawer
            # (ContentSwitcher) that stays collapsed until a menu item opens it
            # downward. Phase 4 fills each pane from its canonical reyn source; each
            # pane is rebuilt from a fresh snapshot when opened (:meth:`_refresh_pane`).
            yield MenuBar(_MENU_TABS, id="menubar", status_text=self._status_text())
            with ScrollableDrawer(initial=None, id="drawer"):
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

    def completion_state(self, text: str) -> CompletionState:
        """The ``/``-command / ``:``-skill completion state for ``text`` (#3354).

        The app's job here is ONLY to resolve the live sources and hand them to
        the pure
        :func:`~reyn.interfaces.inline.textual_chat.completion.compute_completion`
        — no candidate is produced here. The two live sources both come off the
        SAME :class:`~reyn.interfaces.repl.read_model.ChatReadModel` seam every
        other session-local read uses:

        - the local ``Session``
          (:meth:`~reyn.interfaces.repl.read_model.ChatReadModel.completion_session`)
          a command's ``CompleterFn`` is called with, and
        - that session's registered skills (its public
          :meth:`~reyn.runtime.session.Session.available_skills`), the same list
          the ``:`` INVOCATION path filters its ``menu``/``on_demand``/``hidden``
          surface from.

        A remote client holds no session, so BOTH stay ``None`` — which
        ``compute_completion`` reads as "source unavailable" and answers with
        SILENCE, not an empty menu (an empty menu would read as "no such command
        exists"; see that function's ``session``/``skills`` contract). ``/``
        command-name completion is registry-derived and transport-independent, so
        it keeps working there. ``None`` is likewise the answer when the skill
        read RAISES: a failed read is an unavailable source, never an empty one.
        Public because the ``Composer`` calls it as the app's completion hook.
        """
        session = None
        skills: "list | None" = None
        if self._read_model is not None:
            try:
                session = self._read_model.completion_session()
            except Exception:
                logger.exception("textual chat: completion session read failed")
        if session is not None:
            try:
                skills = list(session.available_skills())
            except Exception:
                logger.exception("textual chat: completion skill read failed")
        return compute_completion(text, session=session, skills=skills)

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
        the Help pane — sourced from the binding table itself, not re-typed.

        Handles BOTH shapes Textual accepts in a ``BINDINGS`` list: the
        3-tuple and a full :class:`~textual.binding.Binding` (needed once a
        binding carries a flag, e.g. ``priority=True`` on ``ctrl+c``, #3498).
        A tuple-only reader silently DROPPED such a binding — the key worked
        but the Help pane never mentioned it, which is the single-source-of-
        truth claim in the line above quietly failing rather than erroring."""
        out: list[tuple[str, str]] = []
        for b in self.BINDINGS:
            if isinstance(b, tuple):
                if len(b) >= 3:
                    out.append((b[0], b[2]))
            elif getattr(b, "description", "") and getattr(b, "show", True):
                # ``show`` is Textual's own "advertise this key" flag, and this
                # pane is the advertisement. Honouring it is what lets a
                # binding hand its Help row to a hand-written table (#3818)
                # without a translation step living here.
                out.append((b.key, b.description))
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

    def _artifact_rows(self) -> "list[ArtifactRow]":
        """#4482 PR-3: every listable artifact, newest-first — derived from
        the SAME message source the conversation pane itself renders
        (``self.conversation``, the app's own live ``FlowModel``), never
        ``Session.history``'s resident buffer (invariant 3, repeated
        verbatim across three separate lead-coder dispatches this session:
        the two are not the same list, nor evicted on the same schedule —
        #4387/#4468's byte-cap eviction can drop an entry from
        ``self.history`` while it is STILL visible in the conversation
        pane, and this list must not silently follow that unrelated
        resource-management axis).

        No persistence: :func:`collect_artifact_rows` is pure and only
        reads what is already in each message's own resolved payload
        (`ref`/`name`/`media_type`/`body`). The one piece of I/O this
        method does — resolving each row's `resolved_path` via the SAME
        `resolve_ref` path `_handle_open_artifact_request` itself calls —
        is a review fix (lead-coder/architect, #4482 PR-3): a bare
        `name` is a basename, which cannot distinguish two same-named
        artifacts in different directories, failing the arc's one
        non-negotiable requirement (the user sees the REAL thing about to
        open). Runs only when this method is called (a pane refresh),
        never continuously — "表示ページ分だけ stat"."""
        from pathlib import Path

        from reyn.config import _find_project_root
        from reyn.core.present.artifact_list import (
            collect_artifact_rows,
            resolve_display_paths,
        )
        node_lists = [
            entry.item.meta.get("nodes", [])
            for entry in self.conversation
            if entry.item.kind == "presentation"
        ]
        rows = collect_artifact_rows(node_lists)
        # #4494 design C: THIS call always reflects the live-conversation
        # source (whatever it finds, empty or not) — the fallback is a
        # SEPARATE, async step (:meth:`_maybe_refresh_remote_artifact_fallback`)
        # triggered only after a sync render already ran with this source.
        self._artifact_rows_source = "live"
        if not rows:
            return rows
        project_root = _find_project_root(Path.cwd()) or Path.cwd()
        return resolve_display_paths(rows, project_root, self._agent_name)

    def _pane_rows(self, tab_id: str, snap: "dict | None | object" = _UNSET) -> "list[str]":
        """The display rows for ``tab_id``'s pane, derived from canonical sources:
        the status snapshot (model/agent/cost/ctx), the slash ``REGISTRY`` (menu),
        the live conversation (history), the live artifact list (#4482), and the
        app BINDINGS (help). Pass ``snap`` to reuse an already-read snapshot
        (keeps the rows and the selection ids derived from ONE snapshot)."""
        from reyn.interfaces.slash import REGISTRY  # noqa: PLC0415 — TTY-local
        snapshot = self._snapshot() if snap is _UNSET else snap
        return pane_payload(
            tab_id,
            snapshot=snapshot,  # type: ignore[arg-type]
            commands=REGISTRY.all_commands(),
            history=self._history_turns(),
            artifacts=self._artifact_rows(),
            artifact_source=self._artifact_rows_source,
            artifact_fallback_total=self._artifact_rows_fallback_total,
            app_bindings=self._app_binding_help(),
        )

    def _attach_state(self) -> "str | None":
        """#3671 P3 (B0, shared by the header AND the composer submit-gate —
        see ``on_composer_submitted``): the tri-state
        ``"connecting" | "failed" | None`` (``None`` = attached) read off the
        SAME ``transport.has_session()`` / ``transport.attach_failed()`` seam
        both consumers use, so the two never disagree about whether a session
        is up. Owner ruling: "not yet" and "this is the answer" must never
        render the same — ``None`` here is the ONLY state that lets a
        consumer show a real value; both non-``None`` states must degrade to
        an explicit placeholder, never a stale or fabricated one."""
        if self._transport.has_session():
            return None
        return "failed" if self._transport.attach_failed() else "connecting"

    def _status_text(self, snap: "dict | None | object" = _UNSET) -> str:
        """The Telemetry segment (#4542: ``model · agent    $cost  ctx%``),
        from the live status snapshot (F5b: running cost + context percent are
        visible even with the drawer closed). Falls back to the threaded
        ``agent_name`` pre-session. Pass ``snap`` to reuse an already-read
        snapshot (one read per frame).

        #4542: ``warn_percent`` reads ``self._config.tui.
        context_usage_warn_percent`` via ``getattr`` (same "config may be
        None, or a caller-supplied stand-in missing this section entirely"
        tolerance as every other ``self._config.X`` read in this class —
        see ``_configured_gutter_visibility``'s own docstring for the
        pattern) — falls back to ``status_line_text``'s own module-default
        (:data:`~reyn.interfaces.inline.textual_chat.chrome.
        CTX_WARN_PERCENT`) when unset."""
        snapshot = self._snapshot() if snap is _UNSET else snap
        warn_percent = getattr(
            getattr(self._config, "tui", None),
            "context_usage_warn_percent",
            CTX_WARN_PERCENT,
        )
        return status_line_text(
            snapshot,
            self._agent_name,
            attach_state=self._attach_state(),
            warn_percent=warn_percent,
        )  # type: ignore[arg-type]

    async def _watch_loop_responsiveness(self) -> None:
        """Report ONCE if the event loop stops running on time (#3539).

        Always on. The symptom this watches for arrives unannounced, so an
        opt-in probe would only ever be enabled after the occurrence someone
        wanted to measure — #3638 closed that way. The cost is one float
        comparison per tick against a measured baseline where a 10 ms task
        never exceeded 12 ms over 463 chunks, so a healthy stream never trips
        it.

        The notice is decision-enabling rather than a bare complaint: it says
        how long, and how to record the detail next time.

        It reports on the CHROME, never into the conversation. The first
        version appended a ``kind="system"`` row to the flow, which is a
        category error of the shape the owner already ruled on in #3300: the
        conversation is the record of an exchange, and a watchdog's reading of
        the UI is not part of that exchange. It also had a cost — a row
        inserted at an arbitrary moment shifts every index into the flow, so
        an unlucky 250 ms stall under load broke tests that address entries
        positionally (measured: a 400 ms stall put ``the interface was
        unresponsive for 0.4s`` at ``entries[0]``, exactly the CI failures
        #3668 was carrying). Two surfaces replace it, because they answer
        different questions: the status line for "notice this now", and the
        log for "what happened, read later" — the status line is transient,
        so it alone could not justify a watchdog that is always on.
        """
        import asyncio  # noqa: PLC0415
        import time  # noqa: PLC0415
        from collections import deque  # noqa: PLC0415

        from .loop_probe import (  # noqa: PLC0415
            _TICK_SECONDS,
            stall_banner,
            stall_log_line,
            stall_recovered_log_line,
        )

        # #4761 ② (lead-coder review): a stall that never recovers — #4761's
        # own report, the operator killed the process rather than waiting —
        # never reaches stall_recovered_log_line's own comparison, so H1
        # would be unanswerable on the one occasion it matters most. This
        # loop already wakes every _TICK_SECONDS regardless of stall state,
        # so it can track its OWN trailing window of (wall-clock, pump_ticks)
        # samples and hand the stall notice a self-contained delta — no
        # second event required. 2.0s window: matches _RECORD_INTERVAL_S's
        # own magnitude (loop_probe.py), long enough to span several
        # pump_ticks intervals (1.0s each) so a healthy pump shows a
        # non-zero delta, short enough that the memory here (at most
        # 2.0/_TICK_SECONDS ~= 40 tuples of two floats) is negligible and
        # bounded — old samples are popped every tick, so this deque's own
        # size never grows past that regardless of session length.
        # #4761 ③ reuses this same window (lead-coder: "②で作った形がそのまま
        # 使える") — self._keys_received is sampled alongside pump_ticks at
        # the SAME cadence, so one deque of triples covers both rather than
        # a second, parallel bookkeeping structure. keys_delta lets the
        # stall notice distinguish H3 (pump ticking, zero keys arriving
        # despite an operator who reports pressing several) from H1/H2 on
        # its own, in the same one line — same bound reasoning as ②'s own
        # pump_delta: old samples popped every tick, size never exceeds
        # ~2.0/_TICK_SECONDS regardless of session length.
        _PUMP_WINDOW_S = 2.0
        pump_history: "deque[tuple[float, int, int]]" = deque()

        last = time.perf_counter()
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            now = time.perf_counter()
            lateness_ms = (now - last - _TICK_SECONDS) * 1000
            last = now
            pump_history.append((now, self._pump_ticks, self._keys_received))
            while pump_history and now - pump_history[0][0] > _PUMP_WINDOW_S:
                pump_history.popleft()
            fired = self._loop_tripwire.observe(lateness_ms, pump_ticks=self._pump_ticks)
            if fired is not None:
                pump_delta = (
                    self._pump_ticks - pump_history[0][1] if pump_history else 0
                )
                keys_delta = (
                    self._keys_received - pump_history[0][2] if pump_history else 0
                )
                logger.warning(
                    "textual chat: %s",
                    stall_log_line(
                        fired,
                        pump_ticks=self._pump_ticks,
                        pump_delta=pump_delta,
                        pump_window_s=_PUMP_WINDOW_S,
                        keys_received=self._keys_received,
                        keys_delta=keys_delta,
                    ),
                )
                try:
                    self.notify(stall_banner(fired), severity="warning")
                except Exception:
                    logger.exception("textual chat: loop tripwire notice failed")
            elif self._loop_tripwire.consume_recovered():
                # #4797 follow-up (architect finding): default-visible, no
                # REYN_PROF_DUMP required — everything else this tripwire
                # writes goes through write_record, a no-op on the shipped
                # default. logger.warning, matching the stall notice above
                # (revised from an initial logger.info ruling, self-caught
                # and corrected before landing: the interactive CUI's own
                # _setup_interactive_logging sets the ROOT logger's level to
                # WARNING, so an INFO call from a logger with no override of
                # its own is silently dropped in the real interactive path —
                # not "quieter", genuinely absent. Raising just this logger's
                # level was rejected too: it would make "the operator's
                # chosen floor" mean two different things depending which
                # module emitted the record. Stall and recovery are the
                # start and end of ONE episode; one line per episode at the
                # same severity is not a second alarm).
                logger.warning(
                    "textual chat: %s",
                    stall_recovered_log_line(pump_ticks=self._pump_ticks),
                )

    def on_mount(self) -> None:
        # #3505: #3504 made ``App``'s own background ``ansi_default`` (the
        # terminal's true default), but two chrome regions still painted a
        # concrete ``#0c0c0c`` — the alpha-blend of ``ansi_default`` with any
        # other color drops the "send as terminal default" marker and
        # produces a solid dark RGB instead (Textualize/textual#5452, closed
        # not planned). Textual's own ``"ansi-dark"``/``"ansi-light"`` themes
        # exist for exactly this: every alpha-bearing design-system variable
        # (``$foreground``/``$background``/``$surface``/``$panel``/``$text``/
        # ``$text-muted``/etc.) resolves to ``ansi_default`` (or another
        # marker-carrying ``ansi_*`` value) instead of a literal hex, so the
        # SAME blend that broke under the default theme now blends two
        # marker-carrying values and the marker survives — measured: no
        # ``48;2;`` truecolor background escape codes anywhere in a real
        # terminal capture after this switch, versus 2 residue regions
        # before. A LOCALIZED per-selector ``background: @app-background@;``
        # override (mirroring how #3504 fixed ``App``) was tried first and
        # does NOT work: the literal ``ansi_default`` value fails to
        # propagate when declared on anything other than ``App`` itself
        # (verified with a ``background: red;`` positive control on the same
        # selectors, which DID paint immediately — ruling out a selector/
        # specificity mistake). This theme switch is the only measured fix.
        #
        # Known, accepted trade-off (owner-reviewed, 2026-07-30): every
        # widget that reads a now-``ansi_default``-valued variable loses its
        # concrete-hex identity — most visibly ``$panel``/``$surface`` (the
        # drawer / completion popup / search bar / rewind picker, and
        # ``Composer``'s own opt-out target) go ``transparent`` instead of
        # their prior dark shade, and ``MenuBar``'s own
        # ``color: $text-muted`` / ``:focus-within { color: $text }`` rule
        # collapses to the identical value (partially offset by Tab's own
        # ``:ansi`` variant, which still distinguishes active/inactive via
        # dim/bold text-style). None of this is patched here — see the PR
        # body for the full impact table; MenuBar re-coloring and any
        # ``$panel``/``$surface`` follow-up are separate, out-of-scope
        # issues pending a real-terminal look.
        self.theme = "ansi-dark"
        # #3469 (generalizing #3326's single-key fix): push the COMPLETE
        # palette-derived markdown theme (``renderer.CHAT_MARKDOWN_THEME_STYLES``
        # — the plain renderers' Consoles consume the same constant) onto the
        # app's own Rich console, which Textual's compositor uses to resolve
        # every ``__rich_console__`` render (verified on #3326: this is the
        # actual seam, not merely a plausible one). #3326 overrode ONLY
        # ``markdown.code``, which left every other rich default leaking —
        # H2/H3 headings rendered in rich's "underline magenta" / "bold
        # magenta", the one off-palette colour on the whole screen.
        self.console.push_theme(chat_markdown_theme())
        # Phase 5 (#3273): hydrate the retained model from the PERSISTED
        # conversation log BEFORE the live frame pump starts, so a restart shows
        # the previous conversation (CC ``--resume`` parity) instead of a blank
        # pane. Restored turns render resolved (never RUNNING) through the exact
        # same presenter/gutter path a live frame does. Must run before
        # ``run_worker`` so the prior turns sit ABOVE the first live frame.
        # #3671: bracketed — owner suspected this scales with history size.
        from reyn.runtime.startup_timing import stage  # noqa: PLC0415

        with stage("tui-boot:hydrate"):
            self._hydrate_from_history()
        # The running-blink gutter animates off FlowView's NATIVE animation clock
        # (``animation_fps`` wired in :meth:`compose`), not an app-side timer — so
        # there is nothing to start/pause here. The blink is ADDITIVE: a frozen
        # clock leaves a static, correct amber gutter (see the Phase-2 strip gate).
        # #3671: the startup clock stops HERE — the first moment the interface
        # is on screen and the operator is no longer waiting. Anything measured
        # past this point is the session, not the startup, and folding the two
        # together produced a "first-frame 98.5%" report that was true and
        # useless (it was counting how long someone sat in the chat).
        from reyn.runtime.startup_timing import mark_first_frame  # noqa: PLC0415

        mark_first_frame()
        self.run_worker(self._pump_frames(), name="frames", exclusive=True)
        # #3539: started alongside the frame pump rather than earlier in this
        # method — the worker manager is what makes a coroutine here actually
        # run, and a call placed before the pump's own is silently never
        # awaited (measured: the tripwire stayed at 0.0 ms through a 400 ms
        # synchronous stall).
        self.run_worker(
            self._watch_loop_responsiveness(), name="loop-tripwire", exclusive=False
        )
        # #4761 ②: no ``callback=`` — a Timer with a callback invokes it
        # directly from the Timer's OWN asyncio task (see textual/timer.py's
        # ``_tick``), which would make this just as independent of the App's
        # own message pump as ``_loop_tripwire``'s worker already is. Without
        # one, each tick instead ``post_message``s an ``events.Timer`` onto
        # THIS App's own queue, so :meth:`on_timer` only runs — and
        # ``_pump_ticks`` only advances — when the App's own message-
        # processing loop is actually dequeuing and dispatching, which is
        # the one thing this counter needs to be true to answer H1 (#4761's
        # own architect design: "does the pump keep beating"). 1s: cheap
        # (an int increment), and short enough that even a several-second
        # stall — #4761's own report was long enough to screenshot — has
        # room for multiple ticks if the pump is in fact still alive.
        # Unbounded FOR THE COUNTER's lifetime is fine: nothing here writes
        # anything on its own tick, only reads the counter's current value
        # (see :meth:`_watch_loop_responsiveness`) — the actual OUTPUT stays
        # exactly as bounded as it already was (silent while healthy, two
        # lines per stall episode), this just adds one more field to lines
        # that already exist.
        self._pump_heartbeat_timer = self.set_interval(1.0, name="pump-heartbeat")
        # Drawer starts collapsed — the default chrome is just the focusable
        # menu row (#3326: which also carries the status-values segment when
        # there's room — see MenuBar._repack). It only becomes visible when a
        # menu item is opened (:meth:`_open_drawer`).
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
        # #3476 ④: LAZY page split — only the newest ``_HYDRATE_PAGE_FRAMES``
        # are appended now; the older prefix is held aside and prepended a
        # page at a time by :meth:`on_flow_view_reached_top` as the user
        # scrolls toward it (flowview keeps the scroll position across a
        # prepend). Measured (#3476 issue comment): the view-side win is
        # small at realistic history sizes — this is deliberate owner-chosen
        # forward infrastructure, and the split costs one slice.
        self._older_frames = frames[:-_HYDRATE_PAGE_FRAMES]
        # #4387 Phase B ②: the boundary :meth:`_extend_older_frames_from_disk`
        # cuts future re-projections at — see its own docstring for why this
        # must be a frame count, not the message count ``messages`` carries.
        self._history_frame_count = len(frames)
        tail = frames[-_HYDRATE_PAGE_FRAMES:]
        # #3476 ②: batch append — ``extend`` reflows the view ONCE for the
        # whole appended page instead of once per entry (flowview 0.6.0;
        # the per-entry ``set_state`` calls redraw only the gutter, no
        # reflow). Handle creation cannot fail per-item (presentation runs at
        # paint, not append), so the one try/except covers what the old
        # per-item guard did.
        try:
            entries = self.conversation.extend(tail)
        except Exception:
            logger.exception("textual chat: restore batch append failed")
            return
        for msg, entry in zip(tail, entries):
            _apply_restored_state(msg, entry)
        # #3362: seed the ``/copy`` ring from ALL restored frames — including
        # the not-yet-paged-in older prefix (#3476 ④): what ``/copy`` can
        # reach is the restored HISTORY, not the currently materialised page.
        # #3486: ``appendleft`` in natural (oldest-first) order — the SAME
        # direction the live pump uses — so index 0 is the newest reply AND
        # ``maxlen`` evicts from the OLDEST side. The previous
        # ``reversed(frames)`` + ``append`` expressed newest-first correctly
        # only while the reply count stayed ≤ ``COPY_BUFFER_MAX``: past it,
        # ``append`` evicts from the LEFT — the newest side — silently
        # inverting the "1 = newest" contract for any restored history with
        # more than ``COPY_BUFFER_MAX`` replies. The caller clears the ring
        # alongside ``conversation.clear()``, exactly as it does for the
        # model this loop re-appends to.
        for msg in frames:
            if msg.kind == "agent":
                self._recent_replies.appendleft(msg.text)

    def _extend_older_frames_from_disk(self) -> bool:
        """#4387 Phase B ② (remaining consumers): when ``self._older_frames``
        (#3476 ④'s lazily-held prefix) is exhausted, ask the read model for
        MORE of ``history.jsonl``
        (:meth:`~reyn.interfaces.repl.read_model.ChatReadModel.load_older_conversation_history`)
        before concluding the true start of the conversation was reached —
        closing the gap #4387 Phase B ① opened when ``Session.load_history()``
        stopped necessarily loading the whole file at startup.

        Re-projects the FULL (now possibly longer) message log rather than
        projecting only the newly-read messages: :func:`project_restored_frames`
        is NOT 1:1 with messages (some — ``system``/``summary`` — project to
        nothing; tool-call correlation looks back at the PRECEDING message),
        so slicing the raw message list at the read model's own returned
        count would cut in the wrong place. Slicing the re-projected FRAME
        list at the delta against ``self._history_frame_count`` (the running
        total already accounted for) is the only cut that is correct
        regardless of how projection folds messages. Returns whether any new
        frames became available (``False`` = true start of history, or the
        read model/projection failed — fully guarded, same as
        :meth:`_hydrate_from_history`).

        ``project_restored_frames`` unconditionally prepends ONE
        :data:`.restore.RESUME_DIVIDER` row whenever its own input is
        non-empty — correct for a single call, but this method calls it
        AGAIN on every extension, and flowview has no primitive to REMOVE
        or reposition an already-painted row (only ``append``/``insert``/
        ``insert_many``). So the FIRST divider a non-empty projection ever
        produced (at ``_hydrate_from_history`` time, or an earlier extend)
        is permanent — it stays exactly where it landed. Every LATER
        extend's own fresh divider is a would-be DUPLICATE and is stripped
        before slicing, so the pane only ever carries the ONE divider its
        first non-empty projection produced. This means the divider is NOT
        guaranteed to sit at the true chronological front once extension
        has happened (there is no way to move it there without a removal
        primitive) — an accepted, cosmetic-only limitation: the actual
        conversation CONTENT before and after it is still complete and in
        order, which is what this method's own correctness claim is about.
        """
        if self._read_model is None:
            return False
        try:
            extended = self._read_model.load_older_conversation_history()
        except Exception:
            logger.exception("textual chat: history backward-extend failed")
            return False
        if extended <= 0:
            return False
        try:
            messages = self._read_model.conversation_history()
            frames = project_restored_frames(messages)
        except Exception:
            logger.exception("textual chat: history backward-extend projection failed")
            return False
        divider_already_shown = self._history_frame_count > 0
        if (
            divider_already_shown
            and frames
            and frames[0].kind == "system"
            and frames[0].text == RESUME_DIVIDER
        ):
            content = frames[1:]
            already_known = self._history_frame_count - 1
        else:
            content = frames
            already_known = self._history_frame_count
        new_prefix = content[: len(content) - already_known]
        self._older_frames = new_prefix + self._older_frames
        # The running total this method's OWN slicing bound reads next time
        # always represents the FULL logical projection (divider + every
        # message) — whether or not a duplicate divider was actually
        # painted, exactly one is logically accounted for.
        self._history_frame_count = len(frames)
        return bool(new_prefix)

    def on_flow_view_reached_top(self, event: "FlowView.ReachedTop") -> None:
        """#3476 ④: page the next-older slice of the restored history in when
        the user scrolls near the top. ``insert_many(0, …)`` reflows once and
        flowview keeps the scroll position (the row being read stays put), so
        the page lands invisibly above. Fires once per approach and re-arms on
        retreat (flowview's edge-trigger contract); with nothing left to page
        in this is a no-op. Live frames are unaffected — they append at the
        bottom through the frame pump, never through this path.

        #4387 Phase B ② (remaining consumers): when the held prefix is
        exhausted this no longer concludes "nothing more exists" on the
        spot — it asks :meth:`_extend_older_frames_from_disk` for more of
        ``history.jsonl`` first, so a real conversation start and a merely
        bounded in-memory load are no longer indistinguishable to the user."""
        if not self._older_frames:
            if not self._extend_older_frames_from_disk():
                return
        page = self._older_frames[-_HYDRATE_PAGE_FRAMES:]
        try:
            entries = self.conversation.insert_many(0, page)
        except Exception:
            logger.exception("textual chat: lazy history page-in failed")
            return
        self._older_frames = self._older_frames[:-_HYDRATE_PAGE_FRAMES]
        for msg, entry in zip(page, entries):
            _apply_restored_state(msg, entry)

    # ── #3476 ⑤: in-conversation search ────────────────────────────────────

    def _materialise_all_older(self) -> None:
        """Materialise the ENTIRE lazily-held older prefix (#3476 ④) in one
        ``insert_many`` (one reflow). Search must see the full restored
        history — a hit that exists in history but not in the materialised
        page would read as "no match", a lie — and the measured full-hydrate
        cost is negligible (#3476 issue comment), so search-open simply pays
        it all at once rather than teaching search a second, virtual domain.

        #4387 Phase B ② (remaining consumers): "full restored history" used
        to mean only what #4387 Phase B ① bounded ``load_history()`` loaded
        at startup — a hit older than that tail silently read as "no match"
        too, contradicting this very docstring's promise. Drains
        :meth:`_extend_older_frames_from_disk` to the true start of
        ``history.jsonl`` FIRST, so what gets materialised really is the
        full history, not just the bounded in-memory slice of it."""
        while self._extend_older_frames_from_disk():
            pass
        if not self._older_frames:
            return
        rest = self._older_frames
        try:
            entries = self.conversation.insert_many(0, rest)
        except Exception:
            logger.exception("textual chat: search-open history materialise failed")
            return
        self._older_frames = []
        for msg, entry in zip(rest, entries):
            _apply_restored_state(msg, entry)

    def action_open_search(self) -> None:
        """ctrl+n (#3692 PR-B ③, moved off ctrl+f): open (or refocus) the
        search bar. Reopening keeps the previous query (the browser
        convention), so re-sync its match state."""
        self._materialise_all_older()
        self._search_bar.open()
        if self._search_bar.query:
            self._search_sync(self._search_bar.query, jump=True)
            # #3490: re-opening onto the SAME hit moves the cursor to where it
            # already is, which flowview treats as a no-op (no ``Highlighted``),
            # so the gated rail would not come back on its own.
            self._remark_entry(self._flow.current)

    @staticmethod
    def _search_predicate(query: str):
        """Case-insensitive substring over the MODEL text (``entry.item.text``)
        — what the frame says, independent of presentation. ``entry_text()``
        (the rendered body) is deliberately NOT used: it returns ``""`` for
        entries that have never been painted, which would silently exclude
        every never-scrolled-to row from the search domain."""
        q = query.lower()
        return lambda entry: q in (entry.item.text or "").lower()

    def _search_sync(self, query: str, *, jump: bool) -> None:
        """Recompute the match set for ``query`` and sync the count + the cursor.
        ``jump=True`` moves the cursor to the NEWEST match (a bottom-anchored
        conversation searches backward from now) and centres it; ``jump=False``
        only re-anchors the count around where the cursor already is.

        #3493: search drives the KEYBOARD CURSOR, not a separate selection.
        There is then exactly ONE addressed position in the pane, so two
        different rows can never both be marked — see
        :meth:`_is_addressed_entry`."""
        flow = self._flow
        if not query:
            self._search_bar.set_count(0, 0)
            return
        hits = flow.find(self._search_predicate(query))
        if not hits:
            self._search_bar.set_count(0, 0)
            return
        current = flow.current
        if jump or current not in hits:
            current = hits[-1]
            flow.set_current(current)
            # ``set_current`` only guarantees visibility (minimal scroll); a search
            # hit is centred so the context above and below it is readable.
            flow.scroll_to_entry(current, align="center", animate=True)
        self._search_bar.set_count(hits.index(current) + 1, len(hits))

    def on_search_bar_query_changed(self, event: "SearchBar.QueryChanged") -> None:
        # Incremental: every keystroke recomputes and jumps to the newest
        # match, browser-style.
        self._search_sync(event.query, jump=True)

    def on_search_bar_navigate(self, event: "SearchBar.Navigate") -> None:
        query = self._search_bar.query
        if not query:
            return
        flow = self._flow
        pred = self._search_predicate(query)
        # Recompute lazily AT the navigation (not on every live append): the
        # find_next/find_previous walk runs over the live model, so matches
        # that arrived since the last keystroke are found without any
        # append-time bookkeeping.
        # Origin passed EXPLICITLY: these default to the selection, which this
        # app no longer uses (#3493 — the cursor is the single addressed position).
        target = (
            flow.find_previous(pred, before=flow.current)
            if event.older
            else flow.find_next(pred, after=flow.current)
        )
        if target is None:
            self._search_bar.set_count(0, 0)
            return
        flow.set_current(target)
        flow.scroll_to_entry(target, align="center", animate=True)
        hits = flow.find(pred)
        self._search_bar.set_count(hits.index(target) + 1, len(hits))

    def on_search_bar_dismissed(self, event: "SearchBar.Dismissed") -> None:
        self._search_bar.hide()
        # #3493: the cursor is KEPT on the hit that was found — Shift+Tab back
        # into the pane resumes navigating from there. The rail stops showing
        # because neither gate in :meth:`_is_addressed_entry` holds any more,
        # not because the position was thrown away.
        self._remark_entry(self._flow.current)
        self.query_one(Composer).focus()

    # ── #3490: the addressed-row rail ──────────────────────────────────────

    def _is_addressed_entry(self, entry: "Entry[OutboxMessage]") -> bool:
        """Whether ``entry`` is the row the user is currently addressing — the
        keyboard cursor's position (#3476 ⑥) or the live search hit (⑤).

        Both answer "this is the row you are on", so they share ONE rail rather
        than competing marks; when they coincide the row is marked once. Read by
        :class:`ReynGutter` on every gutter repaint, so it is always the view's
        own live state — ``_flow`` may not exist yet during ``compose``, hence
        the guard.

        Each leg is gated on its own surface actually being ACTIVE, not merely
        on the position being remembered: the cursor's rail shows only while
        FlowView holds focus, the hit's only while the search bar is open. The
        positions themselves persist (leaving and re-entering the pane resumes
        where you were — see :meth:`on_descendant_focus`), but a rail on a pane
        nobody is addressing is permanent chrome rather than an affordance, and
        this whole issue (#3490) is about the mark not intruding on the
        conversation's own design when it has nothing to say."""
        flow = getattr(self, "_flow", None)
        if flow is None or entry is not flow.current:
            return False
        bar = getattr(self, "_search_bar", None)
        return flow.has_focus or (bar is not None and bar.display)

    def _remark_entry(self, entry: "Entry[OutboxMessage] | None") -> None:
        """Re-derive ``entry``'s gutter on the next paint.

        The gutter cache is keyed on the entry's own decor revision, which a
        cursor/selection MOVE does not bump (flowview repaints the body but has
        no reason to think the gutter changed) — so the rail would otherwise
        stay on the row it was first painted on. ``refresh_gutter`` is
        flowview's public invalidation hook for exactly this."""
        if entry is not None:
            try:
                self._flow.refresh_gutter(entry)
            except Exception:
                logger.exception("textual chat: gutter refresh failed")

    def on_descendant_blur(self, event: "events.DescendantBlur") -> None:
        """#3490: hide the cursor's rail when the pane loses focus (Esc back to
        the composer, a Tab step onward). The cursor POSITION is kept — only
        the mark goes, because nothing is being addressed any more."""
        if event.widget is getattr(self, "_flow", None):
            self._remark_entry(self._flow.current)

    def on_flow_view_highlighted(self, event: "FlowView.Highlighted") -> None:
        """#3490: move the rail with the highlight — repaint the row it left as
        well as the one it arrived on.

        #4691 §6 (owner ruling, #4697): highlight movement no longer
        auto-expands/folds tool detail — moving to read the conversation
        and choosing to open ONE row's detail are two different intents,
        and coupling them meant a reader could never move past a big
        result without collapsing it first (#3508's original own
        trade-off). Space (:meth:`_CursorFlowView.action_toggle_fold`,
        outside character-cursor mode) is the dedicated open/close
        trigger now — see ``on_flow_view_toggle_fold_requested``."""
        previous, self._marked_cursor = self._marked_cursor, event.entry
        self._remark_entry(previous)
        self._remark_entry(event.entry)

    def on_flow_view_toggle_fold_requested(self, event: "ToggleFoldRequested") -> None:
        """#4697: Space (outside character-cursor mode —
        :meth:`_CursorFlowView.action_toggle_fold` already applied that
        guard before posting this) flips ONE entry's tool-detail fold,
        independent of the highlight/cursor position — #4691 §6's owner
        ruling.

        #4775 (owner-reported, live TUI): a Group parent (``entry.children``
        truthy — the ONLY call site producing an ``append_child`` is this
        Group construction (#4691 B1), and it only nests a tool row whose
        ``call_id`` matches an already-registered parent, so ``children``
        being non-empty is an exclusive signal — never a false positive on
        some unrelated children-bearing row; a call that dispatches no
        tools never produces a matching tool row to nest, #4779's
        unconditional registration notwithstanding) is not a settled tool
        row and used to
        hit the early return below unconditionally, leaving #4750's collapsed
        child-count display unreachable from Space — the owner's own expected
        trigger. ``Entry.toggle_collapsed()`` (flowview's own fold/unfold
        primitive, already the mechanism ``za`` reaches through flowview's
        OWN z-prefix key handling — this only wires the SAME primitive to
        Space, no new key) is called for a Group parent BEFORE the settled-
        tool-row check, since a Group parent's own ``meta`` has no
        ``_RESULT_KIND_KEY`` and would otherwise never reach a fold path at
        all."""
        entry = event.entry
        if entry.children:
            entry.toggle_collapsed()
            return
        meta = entry.item.meta
        if not meta or _RESULT_KIND_KEY not in meta:
            return  # not a settled tool row — nothing to fold
        self._set_expanded(entry, not bool(meta.get(_EXPANDED_KEY)))

    def _set_expanded(self, entry: "Entry[OutboxMessage] | None", expanded: bool) -> None:
        """Stamp/clear the expansion flag on ``entry``'s ITEM and re-present it.

        #3508 — a settled tool row shows its FULL result when expanded, and its
        one-line summary otherwise. #4697 (owner ruling #4691§6) decoupled this
        from the highlight: Space (:meth:`_CursorFlowView.action_toggle_fold`)
        is the trigger now, not highlight arrival — see
        :meth:`on_flow_view_highlighted`. Two properties make this safe rather
        than a hack:

        * the flag lives on the item, not in this app, because
          ``FlowPresenter.present`` is pure with respect to ``(item, width)`` —
          "expanded" has to BE item state for a differing re-present to be
          legitimate, and ``Entry.update()`` is the sanctioned way to say the
          item changed;
        * only rows that HAVE a folded result are touched, so a user line or an
          agent reply is never marked and never re-presented — moving the
          highlight through ordinary conversation costs nothing.

        The height changes when it unfolds; flowview reflows on ``update()``
        (verified) and the highlight is anchored to the entry, so the row being
        read does not slide."""
        if entry is None:
            return
        meta = entry.item.meta
        if not meta or _RESULT_KIND_KEY not in meta:
            return  # not a settled tool row — nothing folded to show
        if bool(meta.get(_EXPANDED_KEY)) == expanded:
            return  # already in the wanted state; no revision churn
        try:
            # ``OutboxMessage`` is a FROZEN dataclass, so the dict is mutated in
            # place rather than reassigned — which is also the honest shape: the
            # frame's identity does not change, only a display-state key on the
            # meta the presenter already reads.
            if expanded:
                meta[_EXPANDED_KEY] = True
            else:
                meta.pop(_EXPANDED_KEY, None)
            entry.update()
        except Exception:
            logger.exception("textual chat: tool-detail expand failed")

    # ── #3476 ⑥: the keyboard cursor's own actions ─────────────────────────

    def on_descendant_focus(self, event: "events.DescendantFocus") -> None:
        """Arm the keyboard cursor the moment FlowView gains focus (Shift+Tab
        landing on it, #3470), rather than leaving it invisible until the
        first arrow press: flowview's own :meth:`~textual_flowview.FlowView.move_current`
        starts from ``current=None`` and only lands on an entry once a
        direction key moves it — a real but easy-to-miss affordance gap for
        a feature whose whole point is a visible position indicator.
        ``current_last`` (not ``current_first``) so arrival highlights the
        newest entry, matching where a resumed/live conversation's attention
        already is."""
        if event.widget is not self._flow:
            return
        if self._flow.current is None:
            self._flow.current_last()
        else:
            # #3490: the position was remembered from a previous visit, so no
            # cursor MOVE happens and no ``Highlighted`` fires — but the rail is
            # focus-gated, so the row still needs its gutter re-derived to
            # bring the rail back.
            self._remark_entry(self._flow.current)

    async def on_flow_view_key_committed(self, event: "KeyCommitted") -> None:
        """Enter/Space on the cursor entry: copy ITS text directly.

        A direct, ring-free path — ``/copy N`` addresses one of the last
        ``COPY_BUFFER_MAX`` AGENT replies by ordinal; the cursor instead
        points at one exact, arbitrary entry (any kind), so there is no
        ordinal to resolve and no reason to go through the ring.

        #3624: this handles ``KeyCommitted``, NOT upstream's own
        ``FlowView.Selected`` — flowview >=0.11.0 posts ``Selected`` on a
        click too (mouse and keyboard now share one ``current`` cursor), and
        it carries nothing that would let this handler tell a click from an
        Enter/Space press. Reading ``Selected`` directly here would silently
        copy the addressed entry to the clipboard on a stray click, clobbering
        whatever the user had copied from a DIFFERENT application. Deliberately
        not registering ``on_flow_view_selected`` at all — the click case must
        stay a no-op, not a differently-routed copy."""
        from reyn.runtime.outbox import OutboxMessage

        # #3616 ①: pyperclip's plain bool return carries no tool label, so the
        # failure branch gets a real message instead of the empty string the
        # old (ok, tool_label) contract left behind when no tool was found.
        ok = await copy_to_clipboard_async(event.entry.item.text)
        self._ingest_frame(
            OutboxMessage(
                kind="status",
                text="copied to clipboard" if ok else "clipboard copy failed",
            )
        )

    async def on_event(self, event: events.Event) -> None:
        """Any key press or scroll dismisses an active text-effect overlay;
        Esc also dismisses an open rewind picker regardless of focus (#4788).

        The overlay (:meth:`action_toggle_text_effect`) is a full-viewport
        joke painted OVER the flow view; it should end the instant the reader
        does anything else, not only on the exact key (ctrl+l) that started
        it. Intercepted here, at the App's own ``on_event`` — BEFORE Textual's
        normal focus-bubble dispatch — so it fires regardless of which widget
        currently holds focus (composer, flow view, a drawer picker). A
        per-widget ``on_key`` override would only see keys that widget's own
        bubble reaches (composer's Input consumes printable keys itself), and
        the flow view's OWN ``BINDINGS`` (``escape`` → ``cursor_cancel``,
        arrow keys → scroll) would resolve BEFORE the event ever reached a
        handler placed there — this is the one spot that sees the raw event
        first, for every focus target, with no separate hook per widget.

        The dismissing press is consumed and does nothing else: Escape while
        the joke plays closes the joke, not the joke AND cancel a text
        selection; a scroll dismisses it without also moving the flow view.

        #4788: the same "sees the raw event first, regardless of focus" seam
        also closes a real gap in :class:`~.rewind_picker.RewindPicker`'s own
        ``escape`` Binding. That Binding only participates in Textual's
        focused-widget-outward walk when the picker (or a descendant) is
        somewhere in the current focus chain — an intervention arriving while
        the picker is already open steals focus to its own free-text input,
        and Esc then resolves against THAT chain instead, leaving the picker
        visibly open with no way to close it via Esc (found investigating
        #4761: the picker was never stuck — clicking back into it and Enter
        still closed it normally — only its own Esc binding was unreachable).
        Reusing this hook rather than declaring the picker's Binding
        ``priority=True``: a priority Binding is checked DOM-wide before the
        normal walk, so its reach crosses every focus boundary, not just this
        one path — #4751's investigation (architect, same session) flagged a
        live collision risk for exactly that shape (file_access's
        ``RECURSIVE`` ``r`` vs. this module's own ``/rewind`` ``r`` handling
        in :meth:`on_key`, only if either were declared priority=True). This
        stays scoped to one key and one widget instead.

        ★#4788 B (owner-approved, decided after this fix landed): the
        scenario this fix was written for — an intervention arriving
        while the picker is already open — can no longer put both modals
        on screen at once. :meth:`_present_intervention` now closes the
        picker outright the moment an intervention arrives (``RewindPicker
        .hide()``, no Esc involved), rather than leaving it open behind
        the panel. That leaves this Esc branch fully live for one purpose
        only: the picker's own Esc-to-cancel, same as before this fix, now
        simply never racing an intervention's arrival.

        ★Implicit ordering, when BOTH the picker and the intervention panel
        are STILL open at once (the one remaining path: the user opens the
        picker via ``/rewind`` while an intervention panel is already
        showing — #4788 B did not touch that direction, only "intervention
        arrives while picker is open"): this catch runs first and consumes
        the Esc press outright (``event.stop()``), so a single Esc closes
        ONLY the picker — :class:`~.intervention_panel.InterventionPanel`'s
        own ``escape`` Binding (``action_dismiss_panel``) never fires on
        that same press. No capability is lost: the panel's own Esc is
        documented as a focus-only escape hatch, not a close ("every
        pending tab stays exactly as it was" —
        ``InterventionPanel.Dismissed``'s own docstring), so a SECOND Esc
        (picker now closed, this branch a no-op) reaches it exactly as
        before. Picker-first is deliberate — the picker has no other way
        to close once it has lost focus (that is this fix's whole
        premise), while the panel already has one.
        """
        if isinstance(event, events.Key):
            # #4761 ③: counted here — the SAME "sees the raw event first,
            # regardless of focus, regardless of what handling follows"
            # seam this method's own docstring already relies on — not in
            # a per-widget on_key, which only sees keys that widget's own
            # bubble reaches (the composer's Input consumes printable keys
            # itself, so a per-widget count would silently miss most
            # keystrokes). Unconditional: every Key event reaching the App
            # counts, whether it goes on to dismiss an overlay, close the
            # picker, or fall through to ordinary focused-widget dispatch —
            # the counter's only job is "did a key reach the App at all,"
            # not what happened to it next. Deduped by object IDENTITY
            # (see ``_last_counted_key_event``'s own docstring) — measured
            # directly that Textual dispatches the SAME Key object to this
            # method twice for some keys.
            if event is not self._last_counted_key_event:
                self._keys_received += 1
                self._last_counted_key_event = event
        if isinstance(event, (events.Key, events.MouseScrollDown, events.MouseScrollUp)):
            # ``self._flow`` is created lazily in ``compose()``, not
            # ``__init__`` — ``on_event`` fires for every event from the
            # app's own startup/shutdown lifecycle too (Mount, Unmount, …),
            # some of which can arrive before compose has run. ``getattr``
            # rather than a bare attribute access so a Key/Scroll event in
            # that window is a no-op, not a crash.
            flow = getattr(self, "_flow", None)
            if flow is not None and flow.overlay_active:
                flow.stop_overlay()
                event.stop()
                return
        if isinstance(event, events.Key) and event.key == "escape":
            picker = getattr(self, "_rewind_picker", None)
            if picker is not None and picker.display:
                picker.action_dismiss()
                event.stop()
                return
        await super().on_event(event)

    async def on_timer(self, event: events.Timer) -> None:
        """#4761 ②: advance the pump heartbeat for OUR OWN timer only, then
        always delegate to Textual's own base handler.

        ``MessagePump.on_timer`` is what actually invokes a Timer's
        ``callback`` (see ``self._voice_timeout_timer`` /
        ``self._streaming_catchup``, both real ``self.set_timer(...,
        callback=...)`` calls elsewhere in this class) — overriding this
        method without delegating would silently break both. Checking
        ``event.timer is self._pump_heartbeat_timer`` rather than reacting
        to every Timer event keeps this counter meaning ONE specific,
        known-cheap interval, not "any timer anywhere in the app fired,"
        which would make its cadence depend on unrelated widgets' own timer
        churn.
        """
        if event.timer is self._pump_heartbeat_timer:
            self._pump_ticks += 1
        await super().on_timer(event)

    @property
    def pump_ticks(self) -> int:
        """The message-pump heartbeat's current count (#4761 ②) — public
        read, mirroring :attr:`LoopTripwire.max_lateness_ms`/``.fired``'s
        own pattern, so a test or a future caller reads this off the same
        surface the log lines already do rather than the underscore field."""
        return self._pump_ticks

    @property
    def pump_heartbeat_timer(self) -> "Timer | None":
        """The App's own heartbeat :class:`~textual.timer.Timer` — ``None``
        before :meth:`on_mount` starts it. Public so a test can construct a
        genuine ``events.Timer(timer=...)`` referencing the SAME object
        :meth:`on_timer` compares against by identity, without reaching
        into ``_pump_heartbeat_timer`` directly."""
        return self._pump_heartbeat_timer

    @property
    def keys_received(self) -> int:
        """Total Key events this App has ever received (#4761 ③) — public
        read, same pattern as :attr:`pump_ticks`."""
        return self._keys_received

    async def on_key(self, event) -> None:
        # #3476 ⑥: 'r' while the cursor has focus is a keyboard shortcut for
        # typing bare ``/rewind`` + Enter — routed through the exact same
        # ``_submit`` seam an ordinary submission uses, so the picker
        # (#3362's RewindPicker) and rewind's destructive-action path are
        # completely unchanged. NOT a per-entry jump: the conversation pane's
        # ChatMessage.seq and the WAL seq ``list_rewind_points`` addresses are
        # different sequence spaces with no correlation wired anywhere in the
        # codebase today, so a specific cursor entry cannot be mapped onto a
        # specific rewind point without new plumbing outside this PR's scope
        # (recorded on #3476, owner-confirmed: a fast /rewind entry point is
        # the intended integration, not a targeted jump).
        if event.key == "r" and self.focused is self._flow:
            event.stop()
            await self._submit("/rewind")

    def _open_drawer(self, tab_id: "str | None") -> None:
        """Expand/collapse the downward drawer. ``None`` (or the ``"__close__"``
        sentinel) collapses it and returns focus to the composer; a tab id shows
        that pane, focusing the :class:`OptionList` when the pane is an
        interactive picker so ``↑``/``↓`` immediately drive the selection."""
        drawer = self.query_one("#drawer", ContentSwitcher)
        if tab_id is None or tab_id == "__close__":
            drawer.display = False
            drawer.current = None
            self._apply_compact_layout()
            self.query_one(Composer).focus()
            return
        drawer.current = tab_id
        # Rebuild the pane from a fresh snapshot right before it becomes visible,
        # so an opened Model/Agent/Cost/Ctx pane always reflects the CURRENT state
        # (a snapshot read once at compose time would be stale by first open).
        self._refresh_pane(tab_id)
        drawer.display = True
        self._apply_compact_layout()
        child = drawer.query_one(f"#{tab_id}")
        if isinstance(child, OptionList):
            child.focus()
        else:
            # #3699: a readout pane taller than the drawer's cap scrolls — but
            # the scrolling is the DRAWER's (a Static is not a scroll
            # container), so the drawer is what has to hold focus for a key to
            # move it. Without this the content past the fold stays unreachable
            # and merely gains a scrollbar nothing can drive, which is the same
            # defect wearing an affordance.
            drawer.can_focus = True
            drawer.focus()

    def _refresh_pane(self, tab_id: str, snap: "dict | None | object" = _UNSET) -> None:
        """Re-derive ``tab_id``'s pane content from the current canonical sources
        and update the mounted widget in place (``OptionList`` options or the
        ``Static`` text). One snapshot read feeds BOTH the rows and the parallel
        slash commands, so an ``OptionSelected`` maps back to the right command.
        Pass ``snap`` to reuse an already-read snapshot.

        The panes listed in ``chrome._LITERAL_ROW_PANES`` get the SAME
        ``Content``-literal wrap
        :func:`~reyn.interfaces.inline.textual_chat.chrome.build_drawer_pane`
        applies at initial ``compose`` time (:func:`~reyn.interfaces.inline.
        textual_chat.chrome._literal_option_content`) — this refresh path is a
        SEPARATE call site from that initial build (``OptionList.add_options``
        vs the constructor), so it needs its own, independently-verified wrap;
        both ask the one ``pane_needs_literal_rows`` predicate rather than
        re-deciding. For History the row TEXT is additionally neutralized
        upstream, in :meth:`_history_turns`."""
        snapshot = self._snapshot() if snap is _UNSET else snap
        rows = self._pane_rows(tab_id, snapshot)
        # #4574 design B: computed ONCE and reused for both the command list
        # AND the row cache `on_option_list_option_selected` reads for a
        # pure-inline row's materialize+open path — `_artifact_rows()` does
        # real I/O (one `stat()` per ref-bearing row), so a second call here
        # would double that work for no reason.
        artifact_rows = self._artifact_rows()
        self._artifact_rows_cache = artifact_rows
        self._pane_commands[tab_id] = pane_commands(  # type: ignore[arg-type]
            tab_id, snapshot, artifacts=artifact_rows,
            artifact_source=self._artifact_rows_source,
        )
        child = self.query_one(f"#{tab_id}")
        if isinstance(child, OptionList):
            child.clear_options()
            if rows:
                options = (
                    _literal_option_content(rows)
                    if pane_needs_literal_rows(tab_id)
                    else rows
                )
                child.add_options(options)
        elif isinstance(child, Static):
            child.update("\n".join(rows))

    async def _maybe_refresh_remote_artifact_fallback(self) -> None:
        """#4494 design C: once the artifacts pane has rendered its
        LIVE-conversation rows (via :meth:`_refresh_pane`, always
        synchronous — this method never runs before it), consult the
        durable artifact-ref table through the transport when that live
        list came back empty. Covers a remote client (its past turns are
        not on the wire at all) and a local client right after a restart
        (the identical gap, #4584's own measured finding —
        ``restore.project_restored_frames`` has no "presentation" kind
        reconstruction). A non-empty live list is never overridden — it
        carries real ``media_type``/``description`` the ref table cannot
        offer, so this is strictly a fallback, not a merge.

        Re-renders the "artifacts" OptionList in place with the fallback
        rows PLUS the consolidated disclosure
        (``chrome.artifact_fallback_disclosure_text``) appended — always
        appended, even when the fallback itself is empty (#4494's own
        falsify requirement: emptying the ref table empties the rows but
        the disclosure text stays). #4601: the entries this transport
        call returns are ALREADY capped (newest-first, at the one join
        point — ``list_refs_for_agent``) — ``total`` is the pre-cap
        count, threaded into the disclosure's "newest N of M"."""
        if self._artifact_rows_cache:
            return  # live list already had content — no fallback needed
        from pathlib import Path

        from reyn.config import _find_project_root
        from reyn.core.present.artifact_list import (
            resolve_display_paths,
            rows_from_ref_table_entries,
        )

        entries, total = await self._transport.request_artifact_list(agent=self._agent_name)
        fallback_rows = rows_from_ref_table_entries(entries)
        if fallback_rows:
            # Same resolved_path fill-in the live path gets (#4482 PR-3
            # review, "表示から実行まで同じ path を使う") — a bare `name`
            # is a basename, which cannot distinguish two same-named
            # artifacts in different directories.
            project_root = _find_project_root(Path.cwd()) or Path.cwd()
            fallback_rows = resolve_display_paths(
                fallback_rows, project_root, self._agent_name,
            )
        self._artifact_rows_cache = fallback_rows
        self._artifact_rows_source = "ref_table_fallback"
        self._artifact_rows_fallback_total = total
        try:
            drawer = self.query_one("#drawer", ContentSwitcher)
        except Exception:
            return  # not mounted (e.g. a headless/test harness) — nothing to update
        if drawer.current != "artifacts":
            return  # operator navigated away before the fallback arrived
        self._pane_commands["artifacts"] = pane_commands(
            "artifacts", artifacts=fallback_rows, artifact_source="ref_table_fallback",
        )
        rows = pane_payload(
            "artifacts", artifacts=fallback_rows, artifact_source="ref_table_fallback",
            artifact_fallback_total=total,
        )
        child = self.query_one("#artifacts")
        if isinstance(child, OptionList):
            child.clear_options()
            child.add_options(
                _literal_option_content(rows) if pane_needs_literal_rows("artifacts") else rows
            )

    async def on_menu_bar_selected(self, event: "MenuBar.Selected") -> None:
        tab_id = None if event.tab_id == "__close__" else event.tab_id
        self._open_drawer(tab_id)
        if tab_id == "artifacts":
            await self._maybe_refresh_remote_artifact_fallback()

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
        composer.

        **#4574 design B carve-out**: the "artifacts" tab's pure-inline rows
        (`ref is None`, `is_inline` True) carry NO slash command at all —
        there is no OS ref for `/open` to address (see :meth:`_artifact_rows`'s
        own ``ArtifactRow`` docstring) — so the empty-command branch below
        would silently collapse them, same as a genuinely non-actionable row.
        Materializing raw artifact content through the SAME text-command
        pipeline every other row uses is not viable either (arbitrary
        multi-line/binary-ish text as a command argument), so this ONE pane's
        rows are special-cased here, reading :attr:`_artifact_rows_cache`
        (the SAME rows :meth:`_refresh_pane` built alongside the (mostly
        empty, for this pane's inline rows) command list) directly rather
        than going through `self._submit`."""
        tab_id = event.option_list.id
        if tab_id == "artifacts":
            rows = self._artifact_rows_cache
            if 0 <= event.option_index < len(rows):
                row = rows[event.option_index]
                if row.error is None and row.ref is None and row.inline_content is not None:
                    await self._handle_open_inline_artifact_request(row)
                    self._open_drawer(None)
                    return
        cmds = self._pane_commands.get(tab_id or "", [])
        if 0 <= event.option_index < len(cmds) and cmds[event.option_index]:
            await self._submit(cmds[event.option_index])
        self._open_drawer(None)

    async def _handle_open_inline_artifact_request(self, row: "ArtifactRow") -> None:
        """#4574 design B: materialize a pure-inline artifact's content to a
        fresh OS-temp file and open it with the OS default app — the CLIENT
        doing this, never the agent (which may hold no write permission at
        all — a read-only session's only output route for rich content IS
        inline `present`, per the issue's own owner ruling) and never an
        OS-side mint into the `.reyn` artifact-ref store (this content has
        no backing project file for a ref to even point at).

        Owner ruling (#4574, verbatim: "/tmp ファイルにすれば良いのでは"):
        the temp file's retention is the OS's own — reyn does NOT track or
        clean it up (a FOURTH retention axis reyn would then own, which the
        owner explicitly declined). Never `delete=True`: the OS opener is a
        SEPARATE process launched asynchronously (`open_with_os_default`'s
        own `subprocess.Popen`/`os.startfile`) — a delete-on-close temp file
        can vanish before that process even gets to read it.

        `media_type` -> extension: architect's #4574 review, verbatim —
        "the extension is treated as the real permission surface" (`present.
        md:79`), so a media_type this process cannot map to a real extension
        does NOT fall back to opening WITHOUT one (an extension-less file
        resolves to no OS handler on any platform, or worse, an arbitrary
        one) — it reports a status line instead. The content is not lost:
        it already rendered in the conversation body (#4574's own fallback
        render, `present_renderer._render_artifact`)."""
        import mimetypes
        import tempfile

        from reyn.interfaces.repl._open_with_os_default import open_with_os_default
        from reyn.runtime.outbox import OutboxMessage

        ext = mimetypes.guess_extension(row.media_type or "") if row.media_type else None
        if not ext:
            self._ingest_frame(OutboxMessage(
                kind="status",
                text=(
                    f"cannot open {row.name!r} — unknown extension for media_type "
                    f"{row.media_type!r} (content is shown above in the conversation)"
                ),
            ))
            return
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=ext, delete=False, encoding="utf-8",
            ) as fh:
                fh.write(row.inline_content or "")
                temp_path = fh.name
        except OSError as exc:
            self._ingest_frame(OutboxMessage(
                kind="status", text=f"could not write a temp file for {row.name!r}: {exc}",
            ))
            return
        ok = open_with_os_default(temp_path)
        if not ok:
            self._ingest_frame(OutboxMessage(
                kind="status", text=f"could not open {row.name} — no OS opener available",
            ))

    def action_jump_to_latest(self) -> None:
        """Return to the newest output and resume following it (#3712)."""
        try:
            flow = self.query_one(FlowView)
        except Exception:
            return
        flow.scroll_end(animate=False)
        # Cleared HERE rather than waiting on the FollowChanged handler below:
        # ``scroll_end``'s default ``immediate=False`` defers the actual
        # scroll_y update (and so flowview's own follow-state update) until
        # after a screen refresh — the operator just asked to be back at the
        # newest output, so the indicator must go with the keystroke and not
        # a frame later. ``on_flow_view_follow_changed`` still runs once that
        # refresh happens and agrees (idempotent), it simply must not be what
        # the answer waits on.
        self._activity.set_away(False)

    def on_flow_view_follow_changed(self, event: "FlowView.FollowChanged") -> None:
        """The reader left or returned to the newest output (#3712, #3770).

        #3770 follow-up: this REPLACES the old ``watch_scroll_y``-driven,
        per-frame-polled reconstruction (``scroll_target_y >= max_scroll_y``,
        deferred via ``call_after_refresh``) now that flowview 0.14.0 tracks
        and posts this directly (``FlowView.following`` / ``FollowChanged``)
        — the same intent-vs-position distinction #3770 traced as the
        reconstruction's own flaw (a wheel-up during early streaming, with no
        room to move, released nothing under the old geometry check; flowview
        now catches it at the scroll event itself and posts here). Firing on
        every flip, not a per-frame poll, means a quiet-moment scroll-away is
        seen immediately — the exact gap #3712's original fix worked around
        by hooking a reactive watcher instead of a frame.

        Only the "returned" edge needs anything from THIS app: the "left"
        edge does not have to zero or set anything — :meth:`_note_entry_landed`
        already reads ``flow.following`` fresh on each arrival, so there is
        nothing to remember about having left.
        """
        self._activity.set_away(not event.following)

    def _note_entry_landed(self) -> None:
        """One entry arrived — count it, unconditionally.

        Called from the frame pump as the entry lands, i.e. by the producer of
        the event rather than by a later reader of the model: the count says
        how many things happened, and the things themselves are what know.

        No ``following`` check (#3777). The count is a property of the turn,
        not of where the reader is standing, so it does not consult the reader
        at all — which is also what lets its baseline be something the reader
        can see.
        """
        self._turn_entries += 1
        self._activity.set_entries(self._turn_entries)

    def _reset_turn_entries(self) -> None:
        """Start a turn's count at zero. The one place the count is reset."""
        self._turn_entries = 0
        self._activity.set_entries(0)

    def _apply_compact_layout(self) -> None:
        """Re-decide how much room the transient regions may take (#3680).

        Called whenever the answer can change: a resize, or a region opening or
        closing. The decision itself is :func:`compact_caps` — a pure function
        of the height and what is open — so the policy can be read and tested
        without a terminal, and this stays the wiring only.

        Every region it shrinks still holds everything it had: the drawer, the
        picker and the completion popup scroll (#3688/#3699), and the queue
        keeps every item behind its count. Nothing here drops content to make
        room, which is the line #3688 established.
        """
        try:
            queue = self.query_one(SentQueue)
            drawer = self.query_one("#drawer", ContentSwitcher)
        except Exception:
            return  # before compose, or after teardown: nothing to decide
        caps = compact_caps(
            self.size.height,
            drawer_open=bool(drawer.display),
            rewind_open=bool(self._rewind_picker.display),
            completion_open=bool(self._completion.display),
            # ``item_count``, never ``len(rendered_texts())``: the latter is
            # what is ON SCREEN, and while summarised that is one line no
            # matter how many are queued — so the decision would read its own
            # output as its input and flip on every re-decide.
            queue_items=queue.item_count(),
            turn_active=self._activity.state is not None,
            intervention_open=bool(self._iv_panel.display),
        )
        if drawer.display and not caps["drawer"]:
            # Priority 7: there is no height at which this fits and leaves a
            # readable conversation, so it closes rather than becoming a
            # sliver that has taken the conversation with it.
            self._open_drawer(None)
            return
        drawer.styles.max_height = caps["drawer"] or None
        self._rewind_picker.styles.max_height = caps["rewind"] or None
        self._completion.styles.max_height = caps["completion"] or None
        queue.set_summarised(queue.has_items() and not caps["queue"])

    def on_resize(self, event) -> None:
        """A resize changes the whole answer, so re-decide (#3680)."""
        self._apply_compact_layout()

    async def action_cancel_turn(self) -> None:
        """ctrl+c: cooperatively interrupt the in-flight turn (#3498).

        Delegates straight to the transport's ``cancel_inflight`` — the seam
        its own contract named for this key — so local and remote clients
        interrupt through the identical path and this app adds no cancellation
        semantics of its own. A no-op when nothing is running (the runtime
        side fires a per-turn event; with no turn there is nothing to fire).

        Failures are contained and surfaced as a status row rather than
        escaping: an interrupt that raises would leave the user with a turn
        they believe they stopped."""
        try:
            await self._transport.cancel_inflight()
        except Exception as exc:
            logger.exception("textual chat: cancel_inflight failed")
            from reyn.runtime.outbox import OutboxMessage

            self._ingest_frame(
                OutboxMessage(
                    kind="error",
                    text=f"interrupt failed: {type(exc).__name__}: {exc}",
                )
            )

    def _terminal_is_dark(self) -> bool:
        """Whether the terminal's background is dark — asked of the theme, with
        dark as the fallback when there is nothing to ask (mirrors
        ``ActivityRow._terminal_is_dark``, and for the same reason: the answer
        is the app's to give and a latched value would outlive a theme change).
        """
        try:
            return bool(self.current_theme.dark)
        except Exception:  # noqa: BLE001 — a colour choice must not raise
            return True

    def action_toggle_text_effect(self) -> None:
        """Start or stop the full-viewport text effect (#3796).

        The SAME key both ways, and stopping is the library's own
        ``stop_overlay`` — which restores the exact prior view (model, scroll
        position, both cursors untouched). That is what makes the joke safe to
        press: there is no reyn-side state to put back, so there is nothing for
        reyn to put back WRONGLY.

        The feed keeps running underneath. ``FlowView.render_line`` returns the
        overlay line while one is set and never renders the content beneath, so
        arriving output costs what it always costs and is simply not painted
        until the effect stops — no buffering, and nothing to flush on return.
        """
        from reyn.interfaces.inline.textual_chat import text_effect
        from reyn.runtime.outbox import OutboxMessage

        if self._flow.overlay_active:
            self._flow.stop_overlay()
            return
        if not text_effect.available():
            self._ingest_frame(
                OutboxMessage(kind="status", text=text_effect.unavailable_message())
            )
            return
        # ``loop=False``: the factory's generator never ends on its own (it
        # plays forward, rewinds, and repeats), so looping at the library level
        # would never come up. Resize still rebuilds — flowview re-invokes the
        # factory when the viewport dimensions change, which is also what makes
        # a stale cache impossible.
        self._flow.play_overlay(
            text_effect.frame_factory(dark=self._terminal_is_dark()),
            fps=text_effect.DEFAULT_FPS,
            loop=False,
        )

    async def action_voice_toggle(self) -> None:
        """``F2`` — toggle dictation: press to start recording, press again to
        stop, transcribe, and inject the result into the composer (#4187).

        Same key both ways, mirroring :meth:`action_toggle_text_effect`'s
        shape. Unlike that toggle, this one is genuinely stateful across the
        two presses (a mic stream is open in between) — every failure path
        below surfaces a status/error frame and returns rather than raising,
        so a transcription error, a missing extra, or a mic that won't open
        never crashes the app (same promise the retired TUI made, ported).
        """
        from reyn.interfaces.inline.textual_chat import voice as voice_mod
        from reyn.runtime.outbox import OutboxMessage

        if self._voice_busy:
            return  # a transcription is already in flight — ignore the re-press
        # Read once per press (not cached on ``self``): cheap, and it means an
        # operator edit to ``voice.max_duration_s`` takes effect on the VERY
        # NEXT press rather than only for a freshly-constructed recorder.
        voice_cfg = getattr(self._config, "voice", None)
        if self._voice_input is None:
            if voice_cfg is not None and not voice_cfg.enabled:
                self._ingest_frame(
                    OutboxMessage(
                        kind="status",
                        text="voice input disabled in config (set voice.enabled: true)",
                    )
                )
                return
            if not voice_mod.available():
                self._ingest_frame(
                    OutboxMessage(kind="status", text=voice_mod.unavailable_message())
                )
                return
            kwargs: dict = {}
            if voice_cfg is not None:
                kwargs = {
                    "model": voice_cfg.model,
                    "language": voice_cfg.language,
                    "device": voice_cfg.device,
                    "compute_type": voice_cfg.compute_type,
                    "sample_rate": voice_cfg.sample_rate,
                    "cpu_threads": voice_cfg.cpu_threads,
                    "num_workers": voice_cfg.num_workers,
                }
            self._voice_input = voice_mod.VoiceInput(**kwargs)

        # Narrow explicitly: the branch above either returns or assigns, but
        # a type checker does not retain that through a ``self.`` attribute
        # read (unlike a local variable).
        assert self._voice_input is not None
        recorder = self._voice_input
        if not recorder.is_recording:
            try:
                recorder.start_recording()
            except voice_mod.VoiceUnavailable as exc:
                self._ingest_frame(OutboxMessage(kind="error", text=str(exc)))
                return
            self._ingest_frame(
                OutboxMessage(kind="status", text="🔴 recording — F2 to stop")
            )
            # VoiceConfig.max_duration_s's own promise ("auto-cancel recordings
            # longer than this", its inline comment in config/media.py) —
            # unenforced in the retired TUI's base commit this module ports
            # from, but the field exists PRECISELY to bound an operator who
            # forgot a live mic was open, so leaving it declared-but-unread
            # would repeat the exact "voice: parses, nothing consumes it" gap
            # #4187's own issue body raised about the block as a whole.
            max_duration_s = voice_cfg.max_duration_s if voice_cfg is not None else 300.0
            self._voice_timeout_timer = self.set_timer(max_duration_s, self._voice_auto_stop)
            return

        self._voice_cancel_timeout_timer()
        await self._voice_finish_recording(recorder)

    def _voice_cancel_timeout_timer(self) -> None:
        """Disarm the max-duration auto-stop timer, if one is armed.

        Called from every path that ends a recording BEFORE the timer would
        have fired (an ordinary F2-stop, an Esc-cancel) — an armed timer left
        running past that point would fire against a ``VoiceInput`` that is
        no longer recording, which :meth:`_voice_auto_stop` already guards,
        but disarming here is what makes that guard defensive rather than
        load-bearing.
        """
        if self._voice_timeout_timer is not None:
            self._voice_timeout_timer.stop()
            self._voice_timeout_timer = None

    def _voice_auto_stop(self) -> None:
        """``max_duration_s`` elapsed while still recording — stop and
        transcribe exactly as a second F2 press would, so a forgotten
        recording does not grow forever. A no-op if the recording already
        ended some other way (F2 / Esc) before the timer fired — same
        ``is_recording`` guard :meth:`action_voice_toggle` itself uses."""
        self._voice_timeout_timer = None
        recorder = self._voice_input
        if recorder is not None and recorder.is_recording and not self._voice_busy:
            self.run_worker(self._voice_finish_recording(recorder), exclusive=False)

    async def _voice_finish_recording(self, recorder: "VoiceInput") -> None:
        """Stop ``recorder``, transcribe, and either inject the result into
        the composer or report why there is nothing to inject. Shared by the
        ordinary F2-to-stop press and the ``max_duration_s`` auto-stop —
        both end a recording the same way; only what TRIGGERED the stop
        differs."""
        from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

        self._voice_busy = True
        self._ingest_frame(OutboxMessage(kind="status", text="⏳ transcribing…"))
        try:
            text, diag = await recorder.stop_recording()
        except Exception as exc:  # noqa: BLE001 — a bad mic frame must not crash the app
            self._voice_busy = False
            self._ingest_frame(
                OutboxMessage(kind="error", text=f"transcription failed: {exc}")
            )
            return
        self._voice_busy = False
        if not text:
            # Self-diagnosing (ported from the retired module): the peak/rms
            # readout tells the operator whether the mic captured anything at
            # all, so an empty result explains itself instead of reading as a
            # silent no-op.
            reason = diag.get("reason", "silent")
            duration_s = diag.get("duration_s", 0.0)
            if reason == "no_audio" or duration_s < 0.3:
                hint = "no audio captured — mic permission? wrong device?"
            elif reason == "error":
                hint = "transcription error — see logs"
            else:
                peak = diag.get("peak", 0.0)
                hint = f"silent capture: {duration_s:.1f}s, peak={peak:.3f} — check mic gain"
            self._ingest_frame(OutboxMessage(kind="status", text=f"({hint})"))
            return
        self._insert_into_composer(text)

    def _insert_into_composer(self, text: str) -> None:
        """Insert ``text`` at the composer's cursor-head, same placement rule
        as :meth:`_restore_cancelled_text` (#3300 Y-client): prepended even
        when the composer already holds a draft, a newline boundary keeps the
        draft intact, and the cursor lands at the end of ``text`` so typing
        continues right after it. No neutralize here — unlike a cancelled
        SUBMISSION or a sent-queue row (#3302), this text originates from the
        same operator's own mic in the same turn, not from a second render of
        someone else's prior input, so it carries the composer's own ordinary
        trust level (same as typing it).
        """
        composer = self.query_one(Composer)
        existing = composer.text
        composer.text = f"{text}\n{existing}" if existing else text
        lines = text.split("\n")
        row = len(lines) - 1
        col = len(lines[-1])
        composer.move_cursor((row, col))
        self._completion.close()

    def copy_to_clipboard(self, text: str) -> None:
        """#3616②: override ``App.copy_to_clipboard`` so every
        Textual-originated copy goes through reyn's own local-tool sink
        instead of the framework default's raw OSC 52 write.

        Textual calls this from exactly 3 places, none of them FlowView's own
        cursor-based yank (that already routes through :meth:`_write_clipboard`
        via the ``clipboard=`` constructor seam, ``#3507``/``#3692`` — this
        override does not change that path, it only stops being a NO-OP for
        the other 3): ``Screen.action_copy_text`` (the generic mouse-drag
        text-selection copy, bound to ``ctrl+c``/``super+c``), and
        ``TextArea``/``Input``'s own copy actions on a text selection inside
        those widgets. All 3 currently hit the same broken-on-some-terminals
        OSC 52 path ``#3617`` already fixed for the keyboard yank;
        redirecting all 3 to the SAME already-proven-correct sink is the
        identical fix, not a wider blast radius.

        ⚠️ Fixes the SINK only, and — measured after this override was first
        written — does not by itself close #3616②. Two separate gaps stack
        on top of it, both traced with real pilot probes rather than assumed:

        1. **Trigger reachability**: reyn's own ``ctrl+c`` binding
           (``Binding("ctrl+c", "cancel_turn", priority=True)``, ``#3498``,
           owner decision "interrupt unconditionally") consumes ``ctrl+c``
           before ``Screen`` ever sees it — with an active Screen-level
           selection, ``ctrl+c`` calls this method 0 times and
           ``cancel_inflight`` 1 time; ``super+c`` (Cmd+C) calls this method
           1 time. The owner's acceptance machine is Windows + git bash,
           which has no ``super+c`` equivalent.
        2. **Selection itself (#3972, filed, upstream)**: mouse-drag inside
           ``FlowView`` — the conversation pane, where the owner's actual
           copy target lives — does not populate ``Screen.selections`` at
           all (measured: identical drag on a plain ``Static`` widget DOES
           select, via ``get_selected_text()``, using Textual's own
           ``test_selection.py::test_double_width`` incantation; the same
           drag on ``FlowView`` returns ``None``). So even with (1) solved,
           ``FlowView.yank()``'s own body
           (``text = self.screen.get_selected_text() or ""``) has nothing to
           send for a mouse-only selection — this override's sink is never
           reached via that path either. #3972 is a ``textual-flowview``
           (also ``tya5``-owned) integration gap, tracked separately.

        This override still stands on its own: it is unconditionally correct
        for ``TextArea``/``Input`` copy actions (reyn's own ``SearchBar``
        query field, ``InterventionPanel``'s free-text ``Input``), which
        likely DO populate a normal Screen selection (untested here, out of
        this override's own scope) — landing it now closes that slice
        without waiting on #3972 or a decision on revisiting #3498.

        Deliberately NOT ``super().copy_to_clipboard(text)`` (which would
        ALSO fire the OSC 52 write) — dual-writing risks the exact bug this
        fixes: an async/buffered OSC 52 escape sequence reaching the
        terminal after pyperclip already set the OS clipboard correctly
        could overwrite it with the garbled result. ``#3617``'s own
        keyboard-yank sink didn't dual-write either; matching that
        precedent. Still sets ``self._clipboard`` directly (Textual's own
        in-memory bookkeeping ``TextArea``/``Input``'s PASTE actions read
        via ``self.app.clipboard`` for in-session paste-back, independent
        of the OS clipboard this method's SINK choice is about) —
        the one piece of the parent's behavior worth keeping.
        """
        self._clipboard = text
        self._write_clipboard(text)

    def _write_clipboard(self, text: str) -> bool:
        """Yank's clipboard sink: reyn's own local tool, result observable.

        Synchronous by contract (flowview calls it inside ``yank``), so this
        uses the blocking helper rather than the async one — the shell-out is a
        single short-lived subprocess.

        REPORTS its own failure rather than only returning it. The bool is
        returned because the sink contract has one, but nothing upstream reads
        it: flowview's ``action_yank`` calls ``yank()`` and discards the value,
        so a bool alone reaches no one. Without the report a yank onto a machine
        with no clipboard backend is indistinguishable from a yank that worked —
        the user presses ``y``, the selection clears, and nothing says the
        clipboard is unchanged. That is the failure mode #3616 exists for: the
        operator's own report came through THIS path (copy mode ``c`` -> ``y``),
        not the entry copy, and their acceptance test is a real-machine copy on
        a Windows shell where a missing backend is a live possibility.

        Failure only. A successful yank already shows itself — the selection
        clears — so a "copied" line per yank would be noise on the one path a
        user repeats. The wording matches the entry-copy path
        (:meth:`on_flow_view_entry_copied`) so the two sinks do not describe the
        same outcome two different ways."""
        from reyn.runtime.outbox import OutboxMessage

        try:
            ok = copy_to_clipboard(text)
        except Exception:
            logger.exception("textual chat: yank clipboard write failed")
            ok = False
        if not ok:
            self._ingest_frame(
                OutboxMessage(kind="status", text="clipboard copy failed")
            )
        return ok

    def action_close_drawer(self) -> None:
        """``esc`` at the bottom of the ladder (#3806).

        The rungs above this one each own a thing to dismiss — the completion
        popup, the sent queue's focus, the intervention panel — and they stop
        the event when they act. What reaches here is an ``esc`` nobody else
        wanted, so this rung answers "there is nothing to dismiss": close the
        drawer if it is open, otherwise go back to following the newest output.

        **Only when the composer is empty.** With a draft in it, ``esc`` does
        nothing at all rather than moving the view: someone who has typed and
        pressed ``esc`` is most likely reaching for "never mind" on the text,
        and scrolling the conversation instead would answer a question they did
        not ask. ``ctrl+end`` stays the way back that works regardless, which is
        why this rung can afford to be conditional and that one cannot.

        Sending does NOT return to the tail — measured, and left that way
        deliberately (#3806): "did it send" is answered by the sent queue and
        the NOW row, both of which sit OUTSIDE the scrolling region. The
        convention elsewhere (Slack, Discord, a shell) is to jump on send, and
        the reason for it is that those interfaces have nowhere but the scroll
        region to show that the message left. reyn does, so the convention
        arrives here without the thing that justified it.

        **#4187, a new top rung**: an ``esc`` while a voice recording is
        actively open (not merely instantiated — ``VoiceInput`` also exists,
        transcribing, between presses) discards it without transcribing and
        stops here, before the drawer/composer-focus/tail-jump rungs below
        even look at their own state. This mirrors the retired TUI's own
        Esc-cancels-recording behavior, and fits the ladder's own contract:
        an open mic stream is exactly the kind of "thing to dismiss" the
        rungs above are for, and it is silent to every OTHER rung (none of
        them know voice input exists), so it has to claim its own.
        """
        if self._voice_input is not None and self._voice_input.is_recording:
            self._voice_cancel_timeout_timer()
            self._voice_input.cancel()
            from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415
            self._ingest_frame(OutboxMessage(kind="status", text="voice recording cancelled"))
            return
        drawer = self.query_one("#drawer", ContentSwitcher)
        if drawer.display:
            self._open_drawer(None)
            return
        composer = self.query_one(Composer)
        if not composer.has_focus:
            # A rung that predates this one and rode on ``_open_drawer(None)``'s
            # side effect: esc from the conversation pane (or anywhere else that
            # let it bubble) returns focus to the composer — #3365's "esc alone
            # owns 'back' everywhere". It has to be named here now, because the
            # branch above no longer runs when there is no drawer to close, and
            # an unnamed rung is one nobody knows is load-bearing until it is
            # gone. Its own gate caught this within the hour.
            composer.focus()
            return
        if composer.text.strip():
            return
        # Delegated, not reimplemented: ``ctrl+end`` already means exactly this
        # and carries the reasoning for doing both the scroll and the row's
        # state synchronously. A second copy here would be two ways back to the
        # tail, free to drift apart.
        self.action_jump_to_latest()

    def action_focus_conversation(self) -> None:
        """``Ctrl+O``: hand focus to the conversation pane (#3692 PR-B ①).

        Just a focus move — flowview owns everything after that. The current
        entry is whatever flowview already has (freshly opened, or wherever a
        prior visit left it); this does not touch it."""
        self._flow.focus()

    def action_toggle_left_gutter(self) -> None:
        """``ctrl+g`` — flip the LEFT (state-marker) gutter's visibility.

        Delegates straight to flowview's ``toggle_gutter`` (#3352): hiding a
        gutter hands its configured width back to the conversation body and
        reflows, all upstream (``FlowView.body_width`` grows by exactly the
        hidden gutter's width, and the presenter is re-invoked at the new
        width). reyn adds no width arithmetic and no relayout of its own —
        there is nothing here to keep in sync with the library."""
        self._flow.toggle_gutter("left")

    def action_toggle_right_gutter(self) -> None:
        """``ctrl+t`` — flip the RIGHT (elapsed / turn-token) gutter's
        visibility. The right sibling of :meth:`action_toggle_left_gutter`;
        two independent actions because upstream's granularity is two
        independent flags."""
        self._flow.toggle_gutter("right")

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
        are live for the same intervention.

        #4788 B (owner-approved, via lead-coder's recommendation — not
        decided by this session): an arriving intervention closes an
        already-open rewind picker outright, rather than leaving both
        modals live behind each other. Three reasons, all lead-coder's:
        an intervention is the agent BLOCKED and waiting (urgent) while
        the picker is a look-only browsing surface — different priority;
        two simultaneous modals make the Esc key's destination ambiguous
        — exactly the shape #4788 A (fixed in #4789) had to work around
        for Esc specifically, and there is no reason to leave that
        ambiguity standing for every OTHER key too; and what's lost is
        small — the picker holds no state Rewind itself owns, so a single
        ``r`` reopens it.

        Calls :meth:`~.rewind_picker.RewindPicker.hide` directly, NOT
        :meth:`~.rewind_picker.RewindPicker.action_dismiss` — the latter
        posts ``Dismissed``, whose handler (:meth:`on_rewind_picker_
        dismissed`) unconditionally re-focuses the Composer, which is
        the right target for a user's own Esc-cancel but would fight
        this intervention's own focus routing (``InterventionPanel.
        add_pending`` → ``on_tabbed_content_tab_activated`` on a
        first-arriving tab). ``hide()`` clears the picker's display and
        state with no message and no focus side effect, leaving the
        panel's own routing as the only thing deciding where focus lands."""
        picker = getattr(self, "_rewind_picker", None)
        if picker is not None and picker.display:
            picker.hide()
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
        ``_answer_label`` meta key). #3540: this is the LOCAL-panel half of the
        settle — the answer's own broadcast comes back as an
        ``intervention_answer_submitted`` event and stamps the SAME key on the
        SAME entry (:meth:`_handle_intervention_answer_event`), which is what
        settles an answer this panel never saw (`/answer`, an A2A peer, AG-UI
        HITL). Both writes are idempotent in render terms, so the local path
        keeps its immediate feedback without producing a second entry. The
        entry's :class:`EntryState` goes to
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

    async def _handle_copy_request(self, arg: str) -> None:
        """Consume a ``__copy_last_reply__`` sentinel: copy, then report (#3362).

        Delegates the whole resolution + clipboard write to the SHARED
        :func:`~reyn.interfaces.repl._copy_sentinel.handle_copy_sentinel` the
        plain client uses — the arg grammar (``N`` / ``list``) and every
        empty/out-of-range message are therefore identical on both surfaces by
        construction, not by two implementations agreeing today.

        The clipboard write is the EFFECT; the returned ``status`` frame is only
        its report, and is folded into the conversation through the ordinary
        :meth:`_ingest_frame` path so it reads like any other status row. Before
        #3362 this sentinel was in :data:`_SKIP_KINDS`, so BOTH halves were
        missing — the pane showed nothing and the system clipboard was genuinely
        untouched."""
        status = await handle_copy_sentinel(self._recent_replies, arg)
        self._ingest_frame(status)

    async def _handle_open_artifact_request(self, ref: str) -> None:
        """Consume a ``__open_artifact__`` sentinel: resolve the ref, launch
        the OS's own default app on the SAME path, then report (#4482 PR-3).

        Architect's #4482 ruling, verbatim: "開くのに使う path そのものを
        表示し、表示から実行まで同じ path を使う" — the Artifacts pane already
        showed this ref's `name` before the operator selected it; resolving
        the SAME ref here (never a raw path carried by the command itself —
        the artifact payload's own invariant 1) is what keeps "what was shown"
        and "what gets opened" the same string, not two.

        A resolution failure (unknown ref, or the file no longer exists —
        :func:`resolve_ref` returns ``None`` for both, by design; see its own
        docstring) reports as a status line rather than raising — the same
        "report failure, never silently do nothing" shape :meth:`_write_clipboard`
        established for the text-cursor yank path (#3616)."""
        from pathlib import Path

        from reyn.config import _find_project_root
        from reyn.data.workspace.artifact_ref import resolve_ref
        from reyn.interfaces.repl._open_with_os_default import open_with_os_default
        from reyn.runtime.outbox import OutboxMessage

        ref = (ref or "").strip()
        if not ref:
            self._ingest_frame(OutboxMessage(kind="status", text="no artifact ref given"))
            return
        project_root = _find_project_root(Path.cwd()) or Path.cwd()
        resolved = resolve_ref(project_root, self._agent_name, ref)
        if resolved is None:
            self._ingest_frame(OutboxMessage(
                kind="status", text=f"artifact not found (ref={ref})",
            ))
            return
        ok = open_with_os_default(resolved)
        if not ok:
            self._ingest_frame(OutboxMessage(
                kind="status", text=f"could not open {resolved.name} — no OS opener available",
            ))

    def _handle_rewind_request(self, msg: "OutboxMessage") -> None:
        """Consume a ``__rewind_list__`` sentinel: show the picker, or the text
        fallback (#3362).

        The SAME two-legged rule
        :func:`~reyn.interfaces.repl.stream_client.run_output_loop` applies, for
        the same reason:

        - This client HOSTS a command-UI region (the
          :class:`~reyn.interfaces.inline.textual_chat.rewind_picker.RewindPicker`),
          so when the read model carries the structured request
          (``{"kind": "rewind", "points": [...]}``) the picker is populated from
          the POINTS and the sentinel's pre-rendered text is dropped — one list,
          not a list plus a duplicate transcript row. The request is consumed
          (``clear_pending_command_ui``) so it can never be replayed onto a later
          sentinel.
        - No structured request available — the REMOTE case, where command-UI is
          not on the AG-UI wire and ``pending_command_ui()`` is ``None`` by
          design (``read_model.py``) — falls back to appending the sentinel's
          text list. Swallowing it there would trade one silent no-op for
          another, waiting on a picker that can never arrive.

        A ``kind`` other than ``"rewind"`` takes the text fallback too, rather
        than being force-fitted into a rewind picker: command-UI is a typed
        request and this region answers exactly one of its kinds."""
        request = None
        if self._read_model is not None:
            try:
                request = self._read_model.pending_command_ui()
            except Exception:
                logger.exception("textual chat: command-UI read failed")
        points = (request or {}).get("points") if request else None
        if request and request.get("kind") == "rewind" and points:
            self._rewind_picker.show_points(list(points))
            try:
                self._read_model.clear_pending_command_ui()
            except Exception:
                logger.exception("textual chat: command-UI clear failed")
            return
        # persistent kind (not transient "status") so the list stays readable —
        # the same choice the plain client's fallback leg makes.
        self._ingest_frame(replace(msg, kind="intervention"))

    async def on_rewind_picker_point_selected(
        self, event: "RewindPicker.PointSelected"
    ) -> None:
        """A checkpoint was picked: perform the rewind through the ORDINARY
        ``/rewind <seq>`` slash path (#3362).

        Routed via :meth:`_submit`, i.e. the same transport send seam a typed
        ``/rewind <seq>`` uses, which reaches ``rewind_cmd`` → the unified
        ``AgentRegistry.checkout``. This picker therefore has NO private action
        path — the destructive contract (in-flight cancellation, the ``⏪ checked
        out to seq N`` reply, the branch/fork semantics of ``checkout``) is
        defined in exactly one place, and a rewind triggered from here is
        indistinguishable from a typed one."""
        await self._submit(f"/rewind {event.seq}")
        self.query_one(Composer).focus()

    def on_rewind_picker_dismissed(self, event: "RewindPicker.Dismissed") -> None:
        """Esc in the picker: no rewind, focus back to the composer."""
        self.query_one(Composer).focus()

    def _resolve_append_parent(
        self, *, kind: str, meta: dict
    ) -> "Entry[OutboxMessage] | None":
        """The ONE decision of where a NEW entry attaches — a registered
        call_id Group parent, else the current turn's own parent, else
        flat top-level (``None``). #4691 (owner-observed real-machine
        contradiction, root-caused by architect via lead-coder): this used
        to be inlined in :meth:`_ingest_frame` alone, and
        :meth:`_handle_agent_delta_event`'s first-delta entry creation
        called ``self.conversation.append(...)`` directly instead —
        bypassing this decision entirely, so a STREAMED reply (the common
        case for any provider that streams) never nested under its turn
        or registered as a call-level Group parent, no matter what
        :meth:`_ingest_frame` did. Both call sites now go through THIS
        one method (via :meth:`_append_frame`), so the decision cannot
        drift between them again — architect's own framing, via
        lead-coder: "share the PARENT DECISION, not the entry-creation
        procedure" (a streamed reply's own creation — seeding
        :class:`_StreamingReply`, registering visibility tracking — stays
        entirely its own, in :meth:`_handle_agent_delta_event`).

        ① call_id lookup (never "most recently appended" order — see
        :attr:`_call_parents`'s own docstring) — #4691 Phase B B1.
        ② the CURRENT turn's own parent, if one is open — #4691 arc item
        ①, one layer above ①. This is what makes the nesting RECURSIVE
        without extra code: the turn's first ``kind="agent"`` row lands
        here (② fires, ① doesn't yet — it has no call_id parent of its
        OWN), then registers itself as a ``_call_parents`` entry
        (:meth:`_register_call_parent`), so every LATER row for that same
        call_id finds it via ① — user row → call row → tool rows, three
        levels, one mechanism per level.
        ③ flat top-level (``None``) — a legacy/restored row, an op-loop
        caller that never threaded a call_id through, or no turn open.

        ``kind != "user"`` in ② is deliberate, not incidental: a
        ``kind="user"`` frame is never anything OTHER than a turn's own
        parent row (the promotion in :meth:`_handle_turn_started_event`)
        or a standalone intervention-answer fallback row
        (:meth:`_handle_intervention_answer_event`) — neither should ever
        nest under a PRIOR turn's parent. (Whether an intervention-answer
        fallback row landing mid-turn should instead nest under the
        CURRENT turn is an owner-visual call, not decided here.)"""
        call_id = meta.get("call_id")
        parent = self._call_parents.get(call_id) if call_id else None
        if parent is None and kind != "user" and self._current_turn_parent is not None:
            parent = self._current_turn_parent
        return parent

    def _append_frame(self, msg: "OutboxMessage") -> "Entry[OutboxMessage]":
        """Append a NEW entry to the retained model, nested per
        :meth:`_resolve_append_parent` — the ONLY place ``self.conversation
        .append(`` is called for a freshly-created entry (#4691's own
        acceptance line, architect via lead-coder: "zero direct
        ``self.conversation.append(`` call sites left" outside this
        method). Every entry-creating call site — :meth:`_ingest_frame`'s
        normal (non-coalesced) append, and
        :meth:`_handle_agent_delta_event`'s first-delta entry creation —
        goes through this, so the nesting decision is made in exactly one
        place, never two copies that can drift apart."""
        parent = self._resolve_append_parent(kind=msg.kind, meta=msg.meta or {})
        if parent is not None:
            return parent.append_child(msg)
        return self.conversation.append(msg)

    def _register_call_parent(
        self, entry: "Entry[OutboxMessage]", kind: str, meta: dict
    ) -> None:
        """Register ``entry`` as a call-level Group parent when it carries
        a ``call_id`` — #4691 Phase B B1/④, #4777, #4691's own
        streaming-bypass fix (architect via lead-coder). Called from TWO
        sites: :meth:`_ingest_frame`'s normal (non-streaming) append, and
        the streaming-settle branch of that same method, once a streamed
        round's completion frame finally carries its real ``call_id``/
        ``dispatched_tool_calls`` — neither is known any earlier than
        that (a round is not classified as tool-dispatching until it
        actually returns), so the settle leg is the FIRST point
        registration could happen for a streamed round, not a late
        correction.

        #4777 (owner-reported, provider-dependence bug): registration no
        longer gates on ``finish_reason`` — a PROVIDER's own summary
        string, self-reported at its own discretion (litellm passes it
        through verbatim, ``llm.py``'s own ``choices[0].finish_reason``).
        The owner's own provider never returns ``"tool_calls"``, so the
        old gate here never fired on their screen — #4691 Phase B's
        entire Group construction was provider-dependent and silently
        inert for them, despite being green in every test written
        against a provider that DOES report it correctly.

        Registering unconditionally for every ``call_id``-bearing agent
        row is harmless even for an ordinary terminal reply that
        dispatched no tools: nothing ever looks up a call_id belonging
        to a call that dispatched no tools, so an unused entry here is
        dead weight, never a wrong nesting (#4776 tracks this dict's
        own session-lifetime growth separately — not this fix's scope)."""
        call_id = meta.get("call_id")
        if kind != "agent" or not call_id:
            return
        self._call_parents[call_id] = entry
        if meta.get("dispatched_tool_calls"):
            # #4691 Phase B ④: the parent's own spinner starts here — its
            # children are about to arrive RUNNING too, and
            # ``_recompute_parent_state`` (called from every child
            # settle) keeps it RUNNING until every child has settled.
            #
            # #4777: gated on ``dispatched_tool_calls`` — a REYN-OBSERVED
            # fact (router_loop.py stamps it from the LLM result's own
            # ``tool_calls`` list, non-empty precisely when this round
            # actually dispatches tools), not the provider's
            # ``finish_reason`` string. A terminal reply that dispatches
            # no tools registers (harmless, above) but never spins —
            # there is nothing for it to wait on.
            entry.set_state(EntryState.RUNNING)
            # #4691 Phase B item 3 (owner ruling): a completion Group
            # defaults COLLAPSED — called HERE, at registration, even
            # though this entry may still be a LEAF (no child has
            # arrived yet). Before textual-flowview 0.22.0's #14 fix
            # this was a documented no-op on a leaf ("a no-op on a
            # removed entry, a leaf, or when unchanged"), which forced
            # a workaround this comment used to describe (watch the
            # entry's own ``append_child`` call site and re-assert
            # ``.collapse()`` there, guarded to fire exactly once). #14
            # fixed the underlying no-op itself — collapse state is now
            # recorded on ANY live entry, leaf included, and a child
            # appended later "walks its ancestors and is born folded"
            # (release notes, 0.22.0) — so that workaround is gone;
            # this one call is upstream's whole job now. This also
            # means #4691 arc item ①'s own guard (``parent is
            # call_parent``, once needed to keep this collapse from
            # ALSO firing on the TURN parent's first child) has nothing
            # left to guard: the turn parent's own registration
            # (:meth:`_handle_turn_started_event`) never calls this
            # method at all (it is not ``kind="agent"``), so it was
            # never a candidate for this collapse in the first place.
            entry.collapse()

    def _ingest_frame(self, msg: "OutboxMessage") -> "Entry[OutboxMessage] | None":
        """Fold one display frame into the retained model — appending a new entry,
        or COALESCING a correlated tool result into its RUNNING started entry.

        Returns the entry the frame landed in (``None`` for a frame that
        COALESCED into an existing entry rather than creating one — the two
        early-return legs below). #4691 arc item ①: the only consumer of
        this return today is :meth:`_handle_turn_started_event`, which needs
        the freshly-promoted user row itself to record as
        :attr:`_current_turn_parent` — every other call site already
        ignored the old ``None`` return, so widening it costs nothing there.

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
        plain-fallback turn sequence.

        #4380/#4429 originally added a bundling tracker here for
        ``kind="system"`` lifecycle markers carrying ``meta[
        "lifecycle_bundle_key"]`` (``lifecycle_forwarder.on_permission_
        denied``'s marker, the ONE kind owner ruled bundles) — removed by
        #4380 itself, re-measured 2026-08-13: no reachable trigger ever
        produces two adjacent occurrences (``router_loop.py``'s
        ``dispatch()`` runs every tool call SERIALLY, #2344 owner design
        decision, so a ``tool_call_started`` frame always lands between
        any two denials, in both a retry-loop AND a same-round parallel-
        tool_calls path — both traced AND driven through a real TUI to
        confirm). If a real screen ever DOES show several denials in a
        row, that screen is the evidence to redesign against — not a
        speculative comparison rule kept alive for a symptom nobody has
        observed.
        """
        kind = msg.kind
        meta = msg.meta or {}
        # A pipeline's step frames fold into ONE row, keyed by the run. A
        # 15-step run emits 30 of them (a started/completed pair per step), and
        # appended individually they bury the conversation they are progress
        # FOR. ``lifecycle_forwarder`` already sends them as transient
        # ``status`` with the ``run_id`` in meta — the key was there, nothing
        # consumed it.
        if kind == "status" and meta.get("source") == "pipeline":
            if self._coalesce_pipeline_step(msg):
                return
        op_id = meta.get("op_id")
        if kind in ("tool_call_completed", "tool_call_failed") and op_id is not None:
            started = self._running_tools.pop(op_id, None)
            if started is not None:
                self._coalesce_tool_result(started, msg)
                return
        if kind == "agent":
            # #3362: buffer the reply text for ``/copy`` BEFORE the streaming
            # branch below returns early — a streamed reply settles through that
            # return, so appending after it would leave every streamed reply
            # (i.e. the common case) uncopyable. Mirrors the plain client's
            # ``recent_replies.appendleft(msg.text)`` on the same kind.
            self._recent_replies.appendleft(msg.text)
            chain_id = meta.get("chain_id")
            # The completion carries no round, so it settles the LAST round of
            # this chain — the one whose text it holds. Any earlier round is
            # already complete on screen and only needs releasing.
            streaming = self._pop_last_streaming_round(chain_id) if chain_id else None
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
                # #4691 (owner-observed, real-machine — "folding the turn
                # group does not hide the final reply"; architect's own
                # root-cause read, via lead-coder): a STREAMED reply's
                # entry is created in :meth:`_handle_agent_delta_event`,
                # not here — this branch only SETTLES it in place. Its
                # TREE POSITION was already decided at creation time
                # (:meth:`_append_frame`, the shared seam that call also
                # goes through now), so nothing here needs to re-parent
                # it. What DOES still belong here: CALL-LEVEL Group
                # registration (:meth:`_register_call_parent` —
                # ``_call_parents[call_id]``, the RUNNING spinner, the
                # default collapse) was ONLY ever reachable from the
                # non-streaming leg below, which this early ``return``
                # never reaches — so a streamed tool round's own agent
                # row never became a valid Group parent for its OWN tool
                # rows either. ``meta`` here is the TERMINAL completion's
                # own meta (call_id/dispatched_tool_calls/finish_reason)
                # — call_id and dispatched_tool_calls are not known any
                # earlier than this (a round is not classified as
                # tool-dispatching until it actually returns), so this is
                # the first point registration COULD happen, streamed or
                # not — same call as the non-streaming leg, one shared
                # method instead of two copies that can drift apart.
                self._register_call_parent(streaming.entry, kind, meta)
                return
        # #4691 Phase B B1 / arc item ① / owner-observed streaming-bypass
        # fix: nest under this frame's own litellm CALL if one is already
        # registered, else the CURRENT turn's own parent if one is open,
        # else flat top-level (:meth:`_append_frame` — the ONE seam every
        # entry-creating call site in this class goes through, so the
        # nesting decision can never drift between them; see that
        # method's own docstring for the 3-tier rule and why it is
        # shared with :meth:`_handle_agent_delta_event`'s first-delta
        # creation rather than each having its own copy).
        entry = self._append_frame(msg)
        # #3712: an entry just arrived. Counted HERE, by the thing that
        # produced it — not reconstructed later from two reads of the model.
        self._note_entry_landed()
        # #4777 / #4691 Phase B ④ (owner ruling): register this row as a
        # Group parent when it carries a call_id — see
        # :meth:`_register_call_parent`'s own docstring for the full
        # reasoning (provider-independence, the RUNNING spinner, the
        # default-collapse timing). Shared with the streaming-settle leg
        # above, which reaches the SAME call once a streamed round's
        # call_id is finally known.
        self._register_call_parent(entry, kind, meta)
        if kind == "presentation":
            self._begin_image_resolutions(entry, msg)
        if kind == "intervention":
            self._present_intervention(msg, entry)
        else:
            self._apply_lifecycle_state(msg, entry)
        return entry

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
            # #3693: name the tool on the live-turn row, but only from a label
            # the frame actually carries — an unlabelled call stays the generic
            # state rather than inventing a name for it.
            label = (msg.meta or {}).get("label") or (msg.text or "").strip()
            self._activity.specialise(f"TOOL {label}" if label else "WORKING")
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
        if started_meta.get("tool") == "run_pipeline":
            # Settle every open run: the step frames cannot say which of them
            # was the last, and an attached ``run_pipeline`` call owns exactly
            # one run — the same assumption ``lifecycle_forwarder`` unsubscribes
            # on. Any row still open here belongs to a run that has ended.
            for run_id in list(self._pipeline_runs):
                self._settle_pipeline_run(
                    run_id, failed=result_msg.kind == "tool_call_failed"
                )
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
        # #4691 Phase B ④: this settle may be the last of the parent's
        # children — recompute its own spinner state.
        self._recompute_parent_state(started)

    def _recompute_parent_state(self, child: "Entry[OutboxMessage]") -> None:
        """#4691 Phase B ④ — after a child settles, recompute its Group
        PARENT's own state (the spinner #4691 Phase B B1 asked for: RUNNING
        while any of its children still are, SUCCESS/ERROR once all have
        settled).

        A no-op when ``child`` has no parent (an un-nested / top-level
        row — B1's own call_id-lookup miss path, or a restored row). Reads
        ``EntryState`` off every live child via ``entry.state`` — the SAME
        public state the child's own gutter/animation already reflect, so
        this can never disagree with what the child rows themselves show.
        RUNNING wins over any terminal state (a parent with even one
        in-flight child is still in flight); among terminal states ERROR
        wins over SUCCESS (one failed child taints the whole call); CANCELLED
        counts as neither RUNNING nor a taint — an orphan is not a failure
        (#72's own reasoning, reused here at parent granularity)."""
        parent = child.parent
        if parent is None:
            return
        children = parent.children
        if not children:
            return
        states = [c.state for c in children]
        if EntryState.RUNNING in states:
            parent.set_state(EntryState.RUNNING)
        elif EntryState.ERROR in states:
            parent.set_state(EntryState.ERROR)
        else:
            parent.set_state(EntryState.SUCCESS)

    def _coalesce_pipeline_step(self, msg: "OutboxMessage") -> bool:
        """Fold one pipeline step frame into that run's single row.

        Returns ``True`` when the frame was absorbed, ``False`` to let the
        caller append it as an ordinary entry — a frame with no ``run_id`` has
        no row to belong to, and dropping it would lose progress rather than
        tidy it.

        The row shows the run's own state (``rag_ingest.ingest  ▸ 7/15
        transform``) rather than the latest line'"'"'s text, so a reader sees where
        the run IS, not what it most recently said. Progress is read from the
        frame'"'"'s own numbers; the forwarder already puts them in the text, but
        parsing a display string back into numbers is the kind of coupling that
        breaks silently the first time the wording changes.
        """
        meta = msg.meta or {}
        run_id = meta.get("run_id")
        if not run_id:
            return False
        entry = self._pipeline_runs.get(run_id)
        item = replace(msg, meta={**meta, _PIPELINE_RUN_KEY: run_id})
        if entry is None:
            entry = self._flow.append(item)
            self._pipeline_runs[run_id] = entry
            try:
                self._flow.start_entry_animation(entry)
            except Exception:
                logger.exception("textual chat: could not start pipeline animation")
            return True
        try:
            entry.set_item(item)
        except Exception:
            logger.exception("textual chat: could not update pipeline row")
        return True

    def _settle_pipeline_run(self, run_id: str, *, failed: bool) -> None:
        """Stop a run'"'"'s row animating and give it a terminal state.

        Driven by the ``run_pipeline`` tool call completing — the same signal
        ``lifecycle_forwarder`` unsubscribes on — because the step frames
        themselves cannot say "and that was the last one": the final
        ``pipeline_step_completed`` is indistinguishable from any other until
        the tool returns.
        """
        entry = self._pipeline_runs.pop(run_id, None)
        if entry is None:
            return
        try:
            self._flow.stop_entry_animation(entry)
        except Exception:
            logger.exception("textual chat: could not stop pipeline animation")
        entry.set_state(EntryState.ERROR if failed else EntryState.SUCCESS)

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
            # #4691 Phase B ④: an orphan is a settle too — its parent (if
            # any) must not spin forever waiting for a completion that will
            # now never arrive.
            try:
                self._recompute_parent_state(entry)
            except Exception:
                logger.exception(
                    "textual chat: could not recompute orphaned tool's parent state"
                )
        self._running_tools.clear()
        # #4691 Phase B ④: a parent whose children never arrived at all — the
        # turn cancelled between the parent row landing and its first
        # tool_call_started, or every one of its tool_calls was excluded
        # pre-dispatch (#3455) and produced no started entry to track — is
        # NOT in the loop above (nothing to recompute FROM: it has no
        # settled child to trigger it) and would otherwise spin its own
        # RUNNING marker forever. Same CANCELLED verdict as an orphaned
        # tool itself (#72's own reasoning): the turn ending is not a
        # failure of the call, it is the call's report simply never
        # completing.
        for parent in self._call_parents.values():
            if parent.state is EntryState.RUNNING:
                try:
                    parent.set_state(EntryState.CANCELLED)
                except Exception:
                    logger.exception(
                        "textual chat: could not settle an orphaned call-parent"
                    )

    def _settle_turn_parent(self) -> None:
        """#4691 arc item ① — give the CURRENT turn's own parent (the user
        row, :attr:`_current_turn_parent`) its terminal state and release it.

        Called from the ``_TURN_END_EVENT_TYPES`` leg of :meth:`_pump_frames`,
        AFTER :meth:`_sweep_orphaned_running_tools` — by then every
        completion-Group child of this turn already carries a terminal state
        (SUCCESS/ERROR from a normal settle, or CANCELLED from that sweep),
        so this call never races an in-flight child.

        The derivation reuses :meth:`_recompute_parent_state` verbatim, one
        level higher than that method's own usual call sites: passing any one
        of the turn parent's own children makes it recompute states across
        ALL of ``child.parent``'s children (== every completion Group of this
        turn) and set THAT entry's (the turn parent's) state — the same
        RUNNING-wins/ERROR-wins/else-SUCCESS rule, applied one layer up. A
        turn that ends with no completion Group ever having landed under it
        (cancelled before the first call, or every one of its tool_calls
        excluded pre-dispatch) has nothing to recompute FROM — CANCELLED,
        same #72 reasoning as an orphaned call-parent with no settled child,
        never SUCCESS (nothing was observed to call a success)."""
        parent = self._current_turn_parent
        if parent is not None:
            children = parent.children
            if children:
                self._recompute_parent_state(children[0])
            else:
                parent.set_state(EntryState.CANCELLED)
        self._current_turn_parent = None

    def _close_earlier_streaming_rounds(self, chain_id: str, round_index: object) -> None:
        """Finish any record of *chain_id* from a round before *round_index*.

        A new round's first delta is the only signal that the previous round is
        over — its terminal frame arrives once per TURN, not once per round. The
        entry keeps everything it accumulated; it simply stops being a target for
        further text and releases its visibility tracker.
        """
        try:
            stale = [
                key for key in self._streaming_replies
                if key[0] == chain_id and key[1] != round_index
            ]
        except (TypeError, IndexError):  # pragma: no cover - malformed key
            return
        for key in stale:
            record = self._streaming_replies.pop(key, None)
            if record is not None:
                record.release()

    def _pop_last_streaming_round(self, chain_id: str) -> "_StreamingReply | None":
        """Pop the highest-round record for *chain_id*, releasing any others.

        Returns the record the completion frame should settle. Earlier rounds
        are released rather than settled: the completion's authoritative text is
        the LAST message of the turn, so writing it into an earlier entry would
        replace that round's own words with a later round's.
        """
        keys = [key for key in self._streaming_replies if key[0] == chain_id]
        if not keys:
            return None
        last = max(keys, key=lambda k: k[1])
        for key in keys:
            if key != last:
                record = self._streaming_replies.pop(key, None)
                if record is not None:
                    record.release()
        return self._streaming_replies.pop(last, None)

    def _sweep_orphaned_streaming_replies(self) -> None:
        """Release any streamed reply still marked in-flight at a TURN BOUNDARY.

        The sibling of :meth:`_sweep_orphaned_running_tools`, and the same
        argument applies: a record in :attr:`_streaming_replies` means "more
        chunks are coming", and #3530 blinks the row's marker on exactly that.
        The terminal completion frame normally clears it — but that is only ONE
        of the ways a stream ends. ``Ctrl+C`` cancels the turn through the
        transport without any terminal frame, so the record survived and the
        marker blinked forever (owner report, 2026-08-02).

        Fixing the cancel path alone would have left the next one. Both maps are
        therefore released at the SAME boundary the tool sweep already uses:
        once the turn is settled/completed/cancelled there can be no further
        chunks for that turn's reply, whatever ended it — a terminal frame, a
        cancel, an error, or a dropped connection all land on one of those three
        events. That is a property of the turn, not a list of causes to keep
        extending.

        ★ The record is MARKED, never removed. Clearing the map was the first
        attempt and it lost text: the terminal completion frame writes the
        authoritative full body inside ``if streaming is not None``, so a
        turn-end event arriving first would delete the record and skip that
        write entirely (caught by #3570's repaint test, which freezes the clock
        so the final text can only land through that frame). Only the "still
        streaming" claim is withdrawn — which is what the marker reads — and the
        record stays for whatever still needs to find it.
        """
        if not self._streaming_replies:
            return
        for record in self._streaming_replies.values():
            record.settled = True

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

    def _begin_image_resolutions(
        self, entry: "Entry[OutboxMessage]", msg: "OutboxMessage"
    ) -> None:
        """Kick a background fetch (#3846 ②) for every `image` component's
        `src` in a freshly-arrived ``kind="presentation"`` entry.

        Scans ``msg.meta["nodes"]`` (the `present`-op render model) for
        ``component == "image"`` nodes, delegating the actual cache/fetch/
        redraw-on-completion machinery to :meth:`ReynPresenter.
        begin_image_resolution` — this method's own job is only "detect that
        a new frame needs resolving, and hand the presenter the Entry it
        will call ``.update()`` on once settled" (the presenter has no
        reference to any Entry/FlowView on its own; the app owns those).
        Fully guarded, same rationale as :meth:`_begin_running_indicator`:
        a broken image render must never break the frame pump.

        #4464: if at least one image node actually needs resolving (not
        already cached from a prior resolution — same-src reuse across
        entries is real, e.g. a repeated screenshot URL), starts the SAME
        live spinner + elapsed indicator :meth:`_begin_running_indicator`
        already gives a RUNNING tool row — no new visual vocabulary, per the
        owner's explicit "受入条件" for #4464. Each image's
        ``on_settled`` callback re-checks every image src this SAME entry
        still owns; only once none remain unresolved does it stop the
        animation and strip :data:`_RUNNING_SINCE_KEY` (mirroring
        :meth:`_coalesce_tool_result`'s own settle-time stripping) — an
        entry with two images doesn't flip back to static after the FIRST
        one settles while the second is still preparing."""
        try:
            nodes = (msg.meta or {}).get("nodes") or []
            allowed = list(
                getattr(getattr(self._config, "chat", None), "image_url_schemes", None)
                or []
            ) or None
            image_srcs = [
                node.get("src")
                for node in nodes
                if isinstance(node, dict)
                and node.get("component") == "image"
                and isinstance(node.get("src"), str)
                and node.get("src")
            ]
            needs_resolution = [
                src for src in image_srcs if not self._presenter.has_cached_image(src)
            ]
            if needs_resolution:
                self._begin_running_indicator(entry)

            def _on_image_settled(settled_entry: object) -> None:
                if any(not self._presenter.has_cached_image(s) for s in image_srcs):
                    return  # another image on this entry is still resolving
                try:
                    self._flow.stop_entry_animation(settled_entry)  # type: ignore[arg-type]
                except Exception:
                    logger.exception(
                        "textual chat: could not stop image-preparing animation"
                    )
                try:
                    item = settled_entry.item  # type: ignore[attr-defined]
                    stripped = {
                        k: v for k, v in (item.meta or {}).items() if k != _RUNNING_SINCE_KEY
                    }
                    settled_entry.set_item(replace(item, meta=stripped))  # type: ignore[attr-defined]
                except Exception:
                    logger.exception(
                        "textual chat: could not settle image-preparing entry"
                    )

            for src in image_srcs:
                self._presenter.begin_image_resolution(
                    entry, src, allowed_schemes=allowed, on_settled=_on_image_settled,
                )
        except Exception:
            logger.exception("textual chat: could not start image resolution")

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
        # #3693: a client that attached mid-turn knows ``turn_active`` and
        # nothing else — no start instant, no tool, no stream. It says so and
        # shows no clock (``started=False``), rather than timing from the
        # moment it happened to connect.
        if snap.get("turn_active"):
            self._activity.begin("WORKING", started=False)
            # A mid-turn attach did not see the entries that already landed,
            # so it counts from zero and says so by counting from HERE. It
            # cannot report a total it never observed, and a reconstructed
            # number would be the same "baseline nobody saw" defect wearing a
            # different hat.
            self._reset_turn_entries()
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
        ``session_attached`` ``EventFrame``, ``{agent, session_id}``).

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
        # #4691 arc item ① — NOT in :attr:`_PER_SESSION_DICT_STATE` (it is a
        # single ``Entry | None``, not a dict, so the uniform ``.clear()``
        # loop above cannot express it — the exact shape the review flagged
        # as a repeat of #4776's own omission: a per-session field with no
        # explicit reset). ``self.conversation.clear()`` just above already
        # dropped the whole tree this entry belonged to, so a stale
        # reference here is not merely wrong-turn nesting: it is a POINTER
        # INTO A TREE THAT NO LONGER EXISTS.
        self._current_turn_parent = None
        self._iv_panel.collapse_all()
        # #3362, both non-``.clear()``-shaped per-session states this PR adds:
        # the ``/copy`` ring is emptied HERE (next to ``conversation.clear()``,
        # which it shadows) and re-seeded by the hydrate call at the end of this
        # method — an OLD session's replies must not be copyable from the NEW
        # one. The rewind picker is collapsed because its offered checkpoints
        # were listed for the OLD attached session; leaving it up would let a
        # user check out a seq chosen against a conversation no longer on screen.
        self._recent_replies.clear()
        self._rewind_picker.hide()
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
            self._apply_compact_layout()
        elif not applied:
            # #3688: the rejecting branch used to be pure absence — no row, no
            # log, no trace of any kind. "The server dropped it", "the gate
            # superseded it" and "it has not arrived yet" then look identical
            # to the operator AND to anyone investigating, which is what made
            # the owner's report expensive to attribute. The gate rejecting a
            # stale delta is legitimate and stays silent to the operator; it
            # stops being invisible to the LOG, which is the surface an
            # investigation reads.
            logger.debug(
                "textual chat: sent-queue gate rejected user_submitted "
                "msg_id=%s seq=%s (already reflected by a prior snapshot/delta)",
                msg_id, seq,
            )

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
        double-promote an item this app already promoted once.

        #4691 arc item ① (final item): the promoted row also becomes
        :attr:`_current_turn_parent` — this IS the "turn boundary" the arc's
        design notes point to ("no new surface needed — turn boundaries
        already exist on both sides, already consumed elsewhere"). Reset to
        ``None`` FIRST, unconditionally, before the loop below can set a new
        one: a ``turn_started`` for a genuinely NEW turn must never leave a
        PRIOR turn's (possibly still-open, if its own end event was somehow
        missed) parent lying around for this turn's own rows to
        mis-nest under."""
        data = event.data or {}
        chain_id = data.get("chain_id")
        seq = data.get("seq", 0)
        matches = [
            item for item in self._queue_view.queue()
            if item.get("chain_id") == chain_id
        ]
        # #3693: a dispatched turn is the one fact this row is allowed to
        # assert on its own. Set BEFORE the seq gate's early return: a
        # ``turn_started`` this client already reflected is still a turn that
        # is running, and returning early would leave the row hidden through
        # the whole turn.
        self._activity.begin("WORKING")
        self._reset_turn_entries()
        self._current_turn_parent = None
        applied = self._queue_view.apply_turn_started(chain_id=chain_id, seq=seq)
        if not applied:
            return
        from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

        for item in matches:
            msg_id = item.get("msg_id")
            if msg_id:
                self._sent_queue.remove_item(msg_id)
            meta = self._queue_item_meta.pop(msg_id, {}) if msg_id else {}
            # #4691 arc item ④: stamp THIS turn's own chain_id onto the
            # user row itself — the row is created here, at promotion time,
            # before chain_id existed at queue time (a queued item's own
            # meta predates the turn actually starting), so this is the
            # first point it can be threaded through. ReynTurnUsageGutter
            # reads it to show the turn's aggregate token total, now
            # anchored to this row instead of an agent reply (see that
            # class's own docstring for why the two were split).
            meta = dict(meta)
            meta.setdefault("chain_id", chain_id)
            text = _neutralized_label(str(item.get("text", "")))
            entry = self._ingest_frame(OutboxMessage(kind="user", text=text, meta=meta))
            if entry is not None:
                # #4691 arc item ①: owner ruling — "turn Group の親（user
                # 行）そのターンが終わるまで RUNNING" (the turn's own parent
                # stays RUNNING for the WHOLE turn, unlike a completion
                # Group parent, whose RUNNING/settled state is DERIVED from
                # its children as they resolve). Set unconditionally here,
                # not derived, on purpose: deriving it from zero children
                # (there are none yet, at promotion time) would show no
                # state at all, and deriving it incrementally from
                # completion-Group children as they settle mid-turn would
                # flicker this row to SUCCESS between calls, while the turn
                # itself is still very much in flight — settling only
                # happens once, at the turn's own end
                # (:meth:`_settle_turn_parent`).
                entry.set_state(EntryState.RUNNING)
                self._current_turn_parent = entry

    def _announced_intervention_entry(self, iv_id: str) -> "Entry[OutboxMessage] | None":
        """The flow entry ``InterventionHandler.announce`` produced for
        intervention ``iv_id``, or ``None`` when this surface never saw the
        announce (#3540).

        The correlation surface is the ENTRY's own ``meta["intervention_id"]``
        (put there by the handler's ``_iv_meta``), NOT :attr:`_pending_ivs`:
        :meth:`_resolve_intervention` POPS the pending map the moment the TUI
        panel delivers an answer, which is BEFORE the resulting
        ``intervention_answer_submitted`` event comes back round the transport
        — a lookup keyed on the pending map would miss exactly the common
        case. The entry itself is never popped, so it is a stable key at any
        point in the answer's lifecycle."""
        for entry in self.conversation:
            item = entry.item
            if item.kind != "intervention":
                continue
            if (item.meta or {}).get("intervention_id") == iv_id:
                return entry
        return None

    def _handle_intervention_answer_event(self, event) -> None:
        """Fold an ``intervention_answer_submitted`` audit-event into the flow
        entry that ASKED the question (#3300 — the last outbox `kind="user"`
        broadcast site, ``InterventionHandler.deliver_answer_to``, migrated to
        an audit-event; #3540 — the fold).

        #3540: the answer belongs to a question that ALREADY has a flow entry
        (``announce``'s ``kind="intervention"`` row), so appending it as its own
        ``kind="user"`` row left an answered intervention rendering as TWO
        entries live where the SAME session reloaded rendered ONE (``restore.py``'s
        ratified self-contained Q→A shape, #3299 P4). This reads the
        ``intervention_id`` the event has always carried, finds that entry, and
        settles it IN PLACE by stamping ``_answer_label`` — the same churn-zero
        write :meth:`_resolve_intervention` makes for the local-panel path, and
        the same meta key the restore projection writes, so live and restore
        now produce the SAME entry sequence for the same answered intervention.

        The branch is on ENTRY PRESENCE, never on delivery route: `/answer`, an
        A2A peer and the AG-UI HITL path all deliver through the one
        ``deliver_answer_to`` funnel and all of them saw the announce, so one
        entry-keyed lookup covers every funnel uniformly (a TUI-local
        suppression would have lost the answers that never touch the panel).

        FALLBACK — no matching entry: append the bare answer exactly as
        before. That leg is what a thin client which ATTACHED AFTER the
        announce sees, and a bare answer line is the correct render there:
        it has no question row to fold into. It is also the leg the payload's
        RAW text is neutralized on (:func:`_neutralized_label`, the SAME seam
        :meth:`_handle_turn_started_event` uses for ``user_submitted``). The
        fold leg stamps the RAW label instead — deliberately, because
        ``ReynPresenter._present_intervention_pending`` neutralizes
        ``_answer_label`` at ITS render call site (the ONE boundary the
        restored path already relies on), so pre-stripping here would make
        live and restore differ in the stored bytes for no gain.

        Note the ANSWERER's attribution meta (``actor``/``auth_user_id``) is
        not merged onto the folded entry: that entry's ``actor`` is the ASKING
        run's, and history's own answered record keeps no answerer identity
        either — so merging would both overwrite a live field and put live
        ahead of what restore can ever reproduce. The fallback leg carries the
        attribution meta unchanged, as it always did.
        """
        from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

        data = event.data or {}
        raw_text = str(data.get("text", ""))
        iv_id = data.get("intervention_id")
        entry = self._announced_intervention_entry(iv_id) if iv_id else None
        if entry is not None:
            meta = entry.item.meta or {}
            entry.set_item(replace(entry.item, meta={**meta, "_answer_label": raw_text}))
            return
        meta = dict(data.get("meta") or {})
        self._ingest_frame(
            OutboxMessage(kind="user", text=_neutralized_label(raw_text), meta=meta)
        )

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
        row.

        **#3570 — the in-place update is also REPAINT-BUDGETED.** Deltas
        arrive at the provider's rate, not the terminal's; each ``set_item``
        costs a present + a strip render of the WHOLE accumulated body, so at
        a proxy's rate the loop spends itself redrawing frames no eye ever
        separates. A delta arriving within
        :data:`_STREAM_REPAINT_MIN_INTERVAL` of this reply's last repaint
        therefore accumulates WITHOUT a ``set_item``; the next delta past the
        window repaints everything collected since, and a catch-up timer
        (:meth:`_schedule_streaming_catchup`) bounds the wait when no such
        delta follows. Measured on the real TUI path (2000 deltas, 60 KB
        reply, textual-flowview v0.9.0): ``set_item`` 1979 → 75, ``present``
        1908 → 72, wall-clock 16.1 s → 3.3 s, with the full text unchanged.

        This entry is finalized (and popped from
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
        # #3693: content is arriving, so the live-turn row can say so. A
        # refinement of a row that already exists, never a row of its own — a
        # delta outside a turn must not conjure one (``specialise`` no-ops).
        self._activity.specialise("RESPONDING")
        # Correlate on (chain_id, round_index), not chain_id alone. A turn that
        # calls a tool produces MORE THAN ONE assistant message — measured on a
        # real turn: 140 deltas, three tool calls, then 300 deltas, and the two
        # texts land in history as two separate assistant messages (210 and 653
        # chars). Keyed by chain_id alone, the second round's deltas flowed into
        # the entry created before the tool row, so what the model wrote AFTER
        # reading a tool result appeared ABOVE the call that produced it (#3656).
        #
        # ``round_index`` is the producer's own loop counter, not something
        # inferred here: ``_emit_agent_delta`` runs inside the round. Absent (an
        # older producer, or a replayed frame) it reads 0, which reproduces the
        # previous single-entry behaviour rather than failing.
        round_index = data.get("round_index", 0)
        key = (chain_id, round_index)
        self._close_earlier_streaming_rounds(chain_id, round_index)
        existing = self._streaming_replies.get(key)
        if existing is None:
            from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

            # #3712: the reply's own entry, created once when its first delta
            # lands. The 29 deltas that follow fold into it and are not
            # arrivals — one thing arrived, and this is where that is known.
            self._note_entry_landed()
            # #4691 (owner-observed real-machine contradiction, root-caused
            # by architect via lead-coder): THROUGH :meth:`_append_frame`,
            # not a direct ``self.conversation.append(...)`` — this used to
            # append flat, unconditionally, bypassing the SAME nesting
            # decision every other entry-creating call site now shares
            # (:meth:`_resolve_append_parent`), so a streamed reply never
            # nested under its open turn (or, later, under a call-level
            # Group) no matter what the non-streaming path did. call_id is
            # not known yet at this point (a round is not classified as
            # tool-dispatching until it returns), so this lands via the
            # CURRENT TURN tier of that decision when one is open — the
            # call-level Group registration itself happens later, at
            # settle (:meth:`_register_call_parent`, called from
            # :meth:`_ingest_frame`'s streaming-settle branch once the
            # terminal frame's real call_id is known).
            entry = self._append_frame(
                OutboxMessage(kind="agent", text=text, meta={"chain_id": chain_id})
            )
            # The append already carries this first chunk, so rendered == text —
            # and it IS this reply's first repaint, so it opens the #3570 budget
            # window rather than leaving it at 0.0 (which would let the very next
            # delta repaint immediately, however close behind it arrived).
            record = _StreamingReply(
                entry=entry, text=text, rendered=text, last_repaint=self._clock()
            )
            self._streaming_replies[key] = record
            self._track_streaming_visibility(record)
            return
        # ★Accumulate FIRST and unconditionally — the visibility gate below may
        # skip the RENDER, never this line. An off-screen reply that is never
        # re-shown before its completion still had every byte collected here.
        existing.text += text
        if existing.visible:
            self._repaint_streaming_reply_within_budget(existing)

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
        both visibility callbacks share.

        Found by ENTRY IDENTITY within the chain, not by key: a turn now holds
        one record per round (#3656), and the entry's meta carries only the
        chain_id — so a chain_id lookup would find at most one of them and, for
        the others, silently report "not in flight". That is not a miss that
        raises: the record simply keeps its ``visible=True`` default and every
        off-screen delta repaints. Measured exactly that way while making the
        change (an off-screen reply took 2 repaints for 6 deltas where it should
        take 0), which is why the scan is over records rather than a key.

        The identity check is what it always was and still load-bearing: a
        record pointing at a DIFFERENT entry is not this entry's, so a stale
        callback can never write into a successor's row."""
        chain_id = (entry.item.meta or {}).get("chain_id")
        if not chain_id:
            return None
        for key, record in self._streaming_replies.items():
            if key[0] == chain_id and record.entry is entry:
                return record
        return None

    def _is_streaming_entry(self, entry: "Entry[OutboxMessage]") -> bool:
        """Whether ``entry`` is a reply still receiving chunks — what
        :class:`ReynGutter` blinks on (#3530).

        ★ This is a READ of authoritative state, not a timing heuristic. A
        record lives in :attr:`_streaming_replies` from the first delta until
        the TERMINAL COMPLETION FRAME pops it in :meth:`_ingest_frame`, so
        "still open" and "finished" are recorded facts. A model that pauses
        mid-reply therefore keeps blinking, which is the whole point of the
        owner's request — an "idle for N seconds means done" rule would say the
        opposite, and would say it most often exactly when the wait is longest.

        Shares :meth:`_streaming_record_for`'s identity check, so a row whose
        chain_id was reused by a successor entry is not reported as streaming.
        """
        record = self._streaming_record_for(entry)
        return record is not None and not record.settled

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

    def _repaint_streaming_reply_within_budget(
        self, record: "_StreamingReply"
    ) -> None:
        """#3570 — the live leg's repaint decision: flush now, or accumulate and
        let the bound catch up.

        The budget is per reply and measured on the app's own (injectable) clock:
        a delta arriving at least :data:`_STREAM_REPAINT_MIN_INTERVAL` after this
        reply's last repaint flushes everything collected since, in ONE
        ``set_item``. One arriving sooner does not — its text is already on
        :attr:`_StreamingReply.text` (the caller appended it unconditionally,
        which is the data path this method must never touch), so the only thing
        deferred is the redraw.

        ★ The deferral is BOUNDED and the bound does not depend on the producer:
        every skipped repaint arms :meth:`_schedule_streaming_catchup`, so
        accumulated text reaches the screen within one interval even if deltas
        keep arriving forever (the queue never emptying must never mean the
        viewer sees nothing until completion) or stop arriving entirely (a model
        that pauses mid-reply must not leave its last chunk unpainted until the
        completion frame)."""
        if self._clock() - record.last_repaint >= _STREAM_REPAINT_MIN_INTERVAL:
            self._flush_streaming_reply(record)
            return
        self._schedule_streaming_catchup()

    def _schedule_streaming_catchup(self) -> None:
        """Arm the one-shot timer that bounds a #3570 repaint deferral.

        ONE timer for the app, not one per reply: the callback flushes every
        pending in-flight reply, so a second skipped repaint (of this reply or
        another) while one is already armed needs no second timer. The handle is
        cleared by the callback, so the next skipped repaint arms a fresh one —
        the deferral window can therefore never exceed one interval, however long
        the stream runs.

        Guarded: the budget is an OPTIMISATION, so failing to arm the timer must
        degrade to "repaint on the next due delta", never break the pump."""
        if self._streaming_catchup is not None:
            return
        try:
            self._streaming_catchup = self.set_timer(
                _STREAM_REPAINT_MIN_INTERVAL, self._flush_pending_streaming_replies
            )
        except Exception:
            logger.exception("textual chat: could not arm the streamed-reply catch-up")

    def _flush_pending_streaming_replies(self) -> None:
        """The #3570 catch-up timer's callback: repaint every visible in-flight
        reply that owes the viewport text.

        Flushes UNCONDITIONALLY rather than re-consulting the budget — the timer
        was armed by a skipped repaint and fires a full interval later, and a
        budget re-check here could push the same text past yet another window
        (with an injected/frozen test clock, forever). This is the leg that makes
        the deferral bounded, so it must not be able to defer again."""
        self._streaming_catchup = None
        for record in list(self._streaming_replies.values()):
            if record.visible and record.pending:
                self._flush_streaming_reply(record)

    def _flush_streaming_reply(self, record: "_StreamingReply") -> None:
        """Hand the entry the FULL accumulated text and mark it rendered — the
        single place a streamed partial reaches the flow, from the live leg (a
        delta past the #3570 repaint budget), the catch-up timer that bounds that
        budget, or the replay leg (``on_show``).

        ``rendered`` is advanced BEFORE the ``set_item`` on purpose:
        ``set_item`` re-enters flowview (reflow → ``_sync_visibility``), which can
        call straight back into :meth:`_on_streaming_entry_shown`, and a record
        that still looked ``pending`` there would flush a second time."""
        record.rendered = record.text
        record.last_repaint = self._clock()
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
        # #3354: this is a PROGRAMMATIC write, so the composer's key-driven gate
        # already declines to open a menu for it — but a menu the user had open
        # before the delta arrived would keep showing candidates for a token that
        # is no longer under the caret. Close it outright.
        self._completion.close()

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
        RUNNING when its turn ends — a confirmed orphan) and, after it,
        :meth:`_settle_turn_parent` (#4691 arc item ①: give the turn's own
        Group parent — the user row — its terminal state and release
        :attr:`_current_turn_parent`, now that every completion-Group child
        is guaranteed non-RUNNING).

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

        #3300 (event-ify the intervention-answer echo) / #3540 (the fold):
        ``intervention_answer_submitted``
        (:meth:`_handle_intervention_answer_event`) never stages in the
        sent-queue the way ``user_submitted`` does — an intervention answer was
        never a queued inbox item, so there is no promotion step. It does not
        append a row of its own either: it SETTLES the ``kind="intervention"``
        entry its ``intervention_id`` identifies, in place, so an answered
        intervention is ONE Q→A entry (the shape ``restore.py`` already
        projects). Only an answer with no matching entry — a client attached
        after the announce — still appends a bare ``kind="user"`` row.

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
                        # #3693: the turn is over — the row goes, whichever of
                        # the three terminal events arrived. Guarded like its
                        # siblings below: one frame's failure must not stop the
                        # pump, and a chrome row is the last thing that should
                        # be able to.
                        try:
                            self._activity.end()
                        except Exception:
                            logger.exception("textual chat: activity row clear failed")
                        try:
                            self._sweep_orphaned_running_tools()
                        except Exception:
                            logger.exception(
                                "textual chat: orphaned-tool sweep failed"
                            )
                        try:
                            self._sweep_orphaned_streaming_replies()
                        except Exception:
                            logger.exception(
                                "textual chat: orphaned-stream sweep failed"
                            )
                        try:
                            # #4691 arc item ①: settle the turn's own parent
                            # LAST — after both sweeps above have already
                            # given every one of its completion-Group
                            # children a terminal state, so nothing here can
                            # observe a child still RUNNING.
                            self._settle_turn_parent()
                        except Exception:
                            logger.exception(
                                "textual chat: turn-parent settle failed"
                            )
                else:
                    msg = frame.message
                    if msg.kind == "__end__":
                        break
                    # #3362: the two CLIENT-consumed sentinels are handled here,
                    # not skipped. Deliberately NOT written as an early
                    # ``continue`` — the live-chrome refresh at the foot of this
                    # loop must still run for these frames, and a ``continue``
                    # past it is precisely the defect the F5b/#3338 note below
                    # records (an EVENT-leg ``continue`` once starved the whole
                    # status line).
                    elif msg.kind == "__copy_last_reply__":
                        try:
                            await self._handle_copy_request(msg.text)
                        except Exception:
                            logger.exception("textual chat: /copy sentinel failed")
                    elif msg.kind == "__rewind_list__":
                        try:
                            self._handle_rewind_request(msg)
                        except Exception:
                            logger.exception("textual chat: /rewind sentinel failed")
                    elif msg.kind == "__open_artifact__":
                        try:
                            await self._handle_open_artifact_request(msg.text)
                        except Exception:
                            logger.exception("textual chat: /open sentinel failed")
                    elif msg.kind not in _SKIP_KINDS:
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
                    # #3680: the inputs to the layout decision (a turn
                    # starting, an item queued) arrive on these same frames,
                    # so re-deciding here is what keeps the answer from being
                    # computed against state that has not landed yet — the
                    # first version decided at open-time only and was a frame
                    # behind, which measured as one row too many for the
                    # drawer.
                    try:
                        self._apply_compact_layout()
                    except Exception:
                        logger.exception("textual chat: compact layout failed")
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
        already-read ``snap``). #3326: routed through MenuBar (which owns
        placing StatusLine on whichever row has room), not StatusLine directly."""
        try:
            menubar = self.query_one(MenuBar)
        except Exception:
            return  # not yet mounted
        menubar.update_status(self._status_text(snap))

    async def on_composer_submitted(self, event: "Composer.Submitted") -> None:
        text = event.value.strip()
        if not text:
            self.query_one(Composer).clear_and_reset()
            return
        if text in {"/quit", "/exit"}:
            self.query_one(Composer).clear_and_reset()
            await self._transport.shutdown()
            self.exit()
            return
        # #3671 P3 (decision 4B): block ORDINARY submission until attach()
        # completes. Deliberately does NOT clear the composer — the typed
        # text must survive, unlike the two branches above — and does NOT
        # touch `submit_user_text` / the #3300 sent-queue at all: there is no
        # session yet to queue against, so this is a genuinely separate path,
        # not a variant of the queue-cancel "restore text" mechanic. Reuses
        # the SAME `has_session()`/`attach_failed()` pair the header (B0,
        # `_attach_state`) reads, so the two surfaces can never disagree.
        if not self._transport.has_session():
            self._notify_blocked_on_attach()
            return
        self.query_one(Composer).clear_and_reset()
        await self._submit(text)

    def _notify_blocked_on_attach(self) -> None:
        """#3671 P3: tell the operator WHY their Enter did nothing, matching
        the header's connecting/failed distinction (owner ruling: a genuine
        failure must never be papered over as an indefinite "still loading")
        rather than silently dropping the keystroke."""
        if self._transport.attach_failed():
            text = "attach failed (see log) — your message was kept; retry once resolved"
        else:
            text = "still connecting — your message will send once ready"
        from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415
        try:
            self._transport.put_display(OutboxMessage(kind="status", text=text))
        except Exception:
            pass

    async def _submit(self, text: str) -> None:
        """Route one submitted line through the transport send seam.

        #3299 P1: the Composer is now EXCLUSIVELY for new turns — it no longer
        reads ``pending_intervention_head()`` at all. Answering a pending
        intervention (closed-set select or free-text) happens through the
        :class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
        (:meth:`on_intervention_panel_choice_selected` /
        :meth:`on_intervention_panel_text_submitted`) — its own, never-queued
        transport funnel — for anyone who can reach the panel.

        #3595 S5: a ``/``-prefixed line is a COMMAND, and the TUI interprets it
        itself through the layer both reyn clients share
        (:func:`~reyn.interfaces.slash.dispatch.maybe_dispatch_slash`) rather
        than submitting the string for the session to interpret. It is run
        immediately and never queued, which SUBSUMES #3327's ``/answer`` fast
        path: that fix existed because a queued ``/answer`` chases its own
        precondition — the #3300 sent-queue only drains once the blocking turn
        frees, and that turn frees only when the intervention the ``/answer``
        targets resolves — so a keyboard-only user who ``Esc``-dismissed the
        panel (#3299 P1's documented escape hatch, which returns focus WITHOUT
        answering) had no way back at all. That argument was never specific to
        ``/answer``, and a client-side layer has no inbox to queue any command
        on.

        A bare (non-``/``) submission is UNCHANGED: ``submit_user_text``
        durably queues it on the inbox — visible in the sent-queue region
        (#3300 P2b, this module) and cancelable there (#3300 Y-client, ``↑``
        from the composer to focus it when nothing is pending, ``Enter`` on a
        highlighted row to cancel) — rather than losing it. That is the
        invariant #3300 protects, and slash leaving the queue does not touch
        it. Errors are contained and surfaced as an error frame the pump
        renders — a silent input drop is the worst failure for a chat box."""
        try:
            from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
            if await maybe_dispatch_slash(self._transport, text):
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
    ``history.jsonl`` on restart. This ``inline: bool`` PARAMETER is
    unchanged and still selectable by a caller that passes it explicitly;
    #4223 removed ONLY the ``chat.render_mode: inline`` CONFIG value that
    used to drive it here (owner instruction — the config-facing escape
    hatch, not this function's own parameter, which #4223's own invariant
    left untouched). ``alt-screen`` stays the recommended default
    regardless. Returns so the driver's caller can tear the transport down
    + print the cost summary.
    """
    # #4474 (was #3846 ③): this used to eagerly `import
    # textual_image.renderable` — a ONE-TIME, process-global terminal-
    # capability query that had to run strictly BEFORE Textual owned
    # stdin (a real race hazard: `textual_image`'s own docstring warned
    # the query "will not work anymore once Textual is started"). That
    # entire eager-timing concern is GONE: reyn's `HalfBlockImage`
    # (`present_renderer.py`) needs no terminal capability query at
    # all — flowview's own README (0.18.1) states why `textual_image`'s
    # auto-detecting import broke here in the first place (it picks
    # Sixel first, and Sixel occupies ZERO cells in FlowView's
    # virtualized, cell-repainting row model, so FlowView cannot
    # position or clip it), and 0.19.0's own follow-up (Kitty
    # Unicode-placeholder mode is undetectably broken on WezTerm/
    # Konsole — no query exists for "do placeholders actually draw")
    # concluded half-block cells are the only form that needs NO
    # protocol negotiation and renders correctly everywhere. See
    # `HalfBlockImage`'s own docstring for the full chain.

    # #4474: thread the operator-configured fixed image row height (owner's
    # standing rule — no unjustified number embedded without either a
    # reasoning comment or a user-facing override; `ImageConfig`'s own
    # docstring, `config/chat.py`, carries the reasoning). `config` is
    # `None` for a caller with no `ReynConfig` (falls back to
    # `present_renderer.py`'s own module-level default).
    try:
        image_config = getattr(config, "image", None)
        row_height_cells = getattr(image_config, "row_height_cells", None)
        if row_height_cells is not None:
            from reyn.interfaces.repl.present_renderer import (
                set_image_row_height_cells,
            )

            set_image_row_height_cells(row_height_cells)
    except Exception:
        pass

    # #3671: the ``tui-boot`` span begins here. Owner git-bash re-measurement
    # (25.5s startup, tui-boot = 93.7%) showed this span is NOT purely
    # Textual's own boot — reyn's __init__/compose()/on_mount() all run
    # inside it — so it is now broken into named sub-stages
    # (``tui-boot:construct``/``:compose``/``:hydrate``/``:other``,
    # startup_timing.py's ``_TUI_BOOT_NAMED_STAGES``) rather than left as
    # one opaque bracket.
    from reyn.runtime.startup_timing import mark_app_constructed, stage  # noqa: PLC0415

    mark_app_constructed()
    with stage("tui-boot:construct"):
        app = TextualChatApp(
            transport=transport,
            read_model=read_model,
            agent_name=agent_name,
            config=config,
        )
    await app.run_async(inline=inline)
