"""Tagged frame vocabulary for the :mod:`reyn.interfaces.transport` client seam.

The inline CUI historically consumed its session through **two independent
source paths** (ADR-0039 P1): the display outbox (``session.outbox`` → the
registry forwarder → ``repl_outbox`` → ``renderer.message``) and the
audit-event subscription (``session.audit_events`` → ``renderer.on_audit_event``,
which drives the Working / Running / Waiting-for-you indicator). A remote
client, however, sees ONE ordered event stream (AG-UI / SSE, P2). This module
defines the unified, tagged frame vocabulary that both the local
``InProcessTransport`` and any future wire transport present to the client:

- :class:`DisplayFrame` wraps a verbatim :class:`~reyn.runtime.outbox.OutboxMessage`
  (the display path).
- :class:`EventFrame` wraps the renderer-relevant *subset* of audit-events (the
  working-indicator path).

A frame carries its :class:`FrameTag` so the consuming client dispatches to the
renderer's two entry points (``message`` for display, ``on_audit_event`` for
event) at the consuming end — one stream in, two renderer entry points out.

#5830 BLOCKING (lead-coder review, PR #5835's own first pass): ``.tag`` is
ONLY ever safe to read on a genuine :data:`Frame` member (``DisplayFrame``/
``EventFrame``) — :meth:`~reyn.interfaces.transport.client_transport.
ClientTransport.frames` is typed to also yield :class:`BacklogBatch` and
:class:`StatusApplied`, NEITHER of which carries a ``.tag``. **Every**
consumer of that stream must check for both with ``isinstance`` BEFORE
touching ``.tag`` on whatever it got, the same way each already checks for
``BacklogBatch`` — a consumer that does not (``interfaces/repl/stream_
client.py``'s own plain/``--cui`` output loop, before this fix) raises
``AttributeError`` the instant a remote server ever sends a ``STATE_*``
update. ``git grep -n "BacklogBatch" src/reyn`` names every file that must
be checked when a NEW non-``Frame`` stream item is added here — this is
not optional per-consumer discretion, it is the vocabulary's own contract.

The forward-set (:func:`forwarded_frame_kinds`) is mostly **DERIVED** from the
renderer's own vocabulary — ``_WAITING_ON_BY_EVENT`` (the tool-axis table) plus
the turn / intervention-answer events ``on_audit_event`` handles — never
hand-listed. The dual-stream completeness gate
(``tests/interfaces/test_transport_dual_stream_completeness.py``) binds the transport's
coverage to that vocabulary so a renderer event the transport does not forward
fails CI instead of silently vanishing on the wire (the A2 dual-stream bug,
designed out). The ONE deliberate exception is :data:`_STREAMING_EVENTS`
(#3288 ③b, ``agent_delta``) — forwarded ahead of any renderer consumer, by
design: the completeness gate only requires ``consumed ⊆ forwarded``, never
the reverse, so a forwarded-but-not-yet-consumed event is legal, and an EVENT
frame with no handler is silently dropped (not rendered) at the consuming
end — the mechanism ③c later plugged a consumer into
(``TextualChatApp._handle_agent_delta_event``) without ever having risked a
"vanished on the wire" regression in the meantime. The plain/repl renderer
still has no ``agent_delta`` branch — the completeness gate does not require
one (``consumed ⊆ forwarded``, never the reverse).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from reyn.interfaces.repl.status import _WAITING_ON_BY_EVENT

if TYPE_CHECKING:
    from reyn.core.events.events import Event
    from reyn.runtime.outbox import OutboxMessage


class FrameTag(Enum):
    """Which renderer entry point a frame dispatches to at the consuming end."""

    DISPLAY = "display"  # → renderer.message(OutboxMessage)
    EVENT = "event"      # → renderer.on_audit_event(Event)


# The turn-lifecycle + intervention-answer audit-events the renderer's
# ``on_audit_event`` consumes DIRECTLY (i.e. not via the ``_WAITING_ON_BY_EVENT``
# tool-axis table). Kept here next to the derivation so the completeness gate
# has a single, reviewable source for the non-tool half of the vocabulary.
_TURN_AND_ANSWER_EVENTS = frozenset(
    {
        "turn_started",
        "turn_settled",
        "turn_completed",
        "turn_cancelled",
        "user_answered_intervention",
        # #3300 P1 (C): the user-line echo, driven by an event instead of a
        # parallel outbox write (session.submit_user_text). Carries raw text +
        # chain_id + msg_id + attribution meta; each surface's
        # event→display handler neutralizes at render time (see
        # ``reyn.interfaces.repl.renderer.user_submitted_display_message``).
        # #3300 P2a: also carries `seq` (the sent-queue order-race-gate
        # token, see ``Session._bump_queue_seq``).
        "user_submitted",
        # #3300 (event-ify the intervention-answer echo): the LAST site still
        # broadcasting a user-authored line via a ``kind="user"`` outbox
        # frame — ``InterventionHandler.deliver_answer_to`` — migrated to this
        # audit-event, following the ``user_submitted`` precedent exactly.
        # Carries RAW text (the answer's display text: the raw answer, or the
        # matched choice's label) + ``intervention_id`` + attribution ``meta``;
        # each surface neutralizes at ITS render boundary (see
        # ``reyn.interfaces.repl.renderer.intervention_answer_display_message``
        # / ``reyn.interfaces.inline.textual_chat.app.
        # TextualChatApp._handle_intervention_answer_event``). #3540: the
        # Textual surface FOLDS the answer into the ``kind="intervention"``
        # entry ``intervention_id`` identifies rather than appending a row of
        # its own, so for that (now normal) leg the render boundary is
        # ``ReynPresenter._present_intervention_pending``'s ``_answer_label``
        # neutralization — the SAME one the restored Q→A entry passes through;
        # the handler's own ``_neutralized_label`` call still covers the
        # no-matching-entry fallback.
        "intervention_answer_submitted",
        # #3300 P3 (Y-server): cancel-by-id for an UNDISPATCHED (queued) user
        # message — the server-authoritative removal signal (never a
        # client-local "cancel succeeded" response) a client's sent-queue
        # rendering applies, exclusive with `turn_started` for the same
        # msg_id (owner addendum §6a: an item leaves the sent queue via
        # exactly one of these two deltas). Carries `msg_id` + `seq` (the
        # same order-race-gate token) — see ``Session.cancel_queued``.
        "inbox_cancel",
        # #2280: the durability-halt observability surface — emitted (at most
        # once, guarded in ``Session._fail_stop_if_durability_dead`` /
        # ``run_one_iteration``) the moment the session's fail-stop latches, so
        # an operator who is idle (not currently submitting an op) learns the
        # halt proactively instead of only on their next interaction's raised
        # ``DurabilityHaltError``. Carries ``reason`` (e.g.
        # ``"durability_failure"``) — see ``Session.halted_reason``.
        "session_halted",
    }
)


# #3310 N1: the session-switch notification — a stream BARRIER the registry
# attach seam (``AgentRegistry.attach``/``attach_session``) puts directly on
# ``repl_outbox`` (never routed through a session's own audit-events — see
# ``AgentRegistry._announce_session_attached``). Forwarded ahead of any
# consumer, exactly like :data:`_STREAMING_EVENTS` below: a client resets its
# per-session display cache on this event (N2, a separate PR); until that
# consumer lands, a surface with no branch for it drops it silently (opt-in
# draw), never a garbage row.
_SESSION_LIFECYCLE_EVENTS = frozenset({"session_attached"})


# #3288 ③b: streamed LLM content-delta audit-events — the owner-ratified L4
# replacement (issue #3288 comment thread): a partial rides an audit-event
# (never an ``OutboxMessage`` kind, which the closed display vocabulary would
# have to register — the category error the owner's decision designs out).
# UNLIKE :data:`_TURN_AND_ANSWER_EVENTS` above, this was forwarded AHEAD OF
# any consumer — ③c has since added the textual_chat coalescing handler
# (``TextualChatApp._handle_agent_delta_event``), but the plain/repl renderer
# still branches on nothing for it (and may never). This is legal per the
# dual-stream completeness gate's actual direction (``tests/interfaces/test_transport_dual_stream_completeness.py``:
# ``consumed ⊆ forwarded``, never the reverse) — a forwarded event nobody
# consumes yet is not a coverage gap, and a surface with no handler for an
# EVENT frame consumes-but-drops it (never renders it), unlike an unknown
# DISPLAY kind (which a presenter renders generically) — see the ③b PR body
# for the frame-level witness of that "no visible-garbage window" property.
_STREAMING_EVENTS = frozenset({"agent_delta"})


@lru_cache(maxsize=1)
def forwarded_frame_kinds() -> frozenset[str]:
    """The set of frame kinds the transport forwards onto the unified frame
    stream (both ``InProcessTransport`` and the AG-UI endpoint filter against
    this). Deliberately NOT "audit-event types": most members ARE real
    audit-events (``EventLog``-backed), but ``session_attached`` is not — it's
    an ``EventFrame`` the registry attach seam puts directly on
    ``repl_outbox``, never touching ``.reyn/events`` (#3794 P1). A name
    claiming audit-event provenance for this set would be the same factual
    error P1 fixed, restated.

    Union of:

    - ``_WAITING_ON_BY_EVENT.keys()`` (``interfaces/repl/status.py``) — the
      tool-axis WaitingOn transition table (``tool_called`` / ``tool_returned``
      / ``tool_failed``); extending WaitingOn to a new axis is one new entry
      there and this set follows automatically.
    - :data:`_TURN_AND_ANSWER_EVENTS` — the turn-lifecycle / intervention-answer
      / user-submitted events ``renderer.on_audit_event`` branches on directly
      (DERIVED from the renderer's own vocabulary, never hand-listed for this
      half — see ``tests/interfaces/test_transport_dual_stream_completeness.py``).
    - :data:`_STREAMING_EVENTS` (#3288 ③b) — the ONE deliberate exception to
      "derived, not hand-listed": forwarded ahead of any consumer in THIS
      (plain/repl) renderer, which still has no ``agent_delta`` branch and
      may never — an unconsuming surface silently drops it (opt-in draw)
      rather than it vanishing on the wire. ③c has since added the actual
      consumer in a DIFFERENT surface (``TextualChatApp._handle_agent_delta_event``,
      ``interfaces/inline/textual_chat/app.py``), proving the forward-ahead
      design worked: the consumer landed with zero changes needed here.
    - :data:`_SESSION_LIFECYCLE_EVENTS` (#3310 N1) — the ``session_attached``
      switch-barrier, a SECOND forward-ahead-of-consumer exception for the
      same reason as ``_STREAMING_EVENTS``: no renderer branches on it yet
      (the client-side reset is N2, a separate PR).
    """
    return (
        frozenset(_WAITING_ON_BY_EVENT.keys())
        | _TURN_AND_ANSWER_EVENTS
        | _STREAMING_EVENTS
        | _SESSION_LIFECYCLE_EVENTS
    )


@dataclass(frozen=True)
class DisplayFrame:
    """A display-path frame: one verbatim outbox message → ``renderer.message``.

    #5041 ①: ``agent`` is the ORIGIN agent's name — architect's own reading
    (issuecomment-5442805752) found this frame carried no attribution at
    all, structurally, so a consumer draining a stream that N agents'
    frames converge onto (e.g. the registry's process-wide ``repl_outbox``,
    #5041's own motivating case) could not tell them apart, even in
    principle. Not a new concept: :class:`BacklogBatch` below already
    carries ``agent`` on the snapshot/reconnect path (#5139) — this is that
    same axis threaded onto the live per-frame path too. Optional / defaults
    to ``None`` so every existing construction site (there are many, across
    the AG-UI wire encode/decode paths and test doubles) is unaffected;
    :class:`~reyn.interfaces.transport.in_process.InProcessTransport` is the
    one production path that populates it today (from the registry's
    already-tracked attached-agent name), closing ①'s finding for the
    ``repl_outbox`` convergence point specifically. Scope note (disclosed,
    not silently dropped): the SAME structural gap exists for an
    ``EventFrame`` sourced from a session's own audit-event subscription
    outside that convergence point — left for a follow-up, per the owner
    dispatch's own explicit "①だけ、広げない" scoping."""

    message: "OutboxMessage"
    tag: FrameTag = FrameTag.DISPLAY
    agent: "str | None" = None


@dataclass(frozen=True)
class EventFrame:
    """An event-path frame: one renderer-relevant audit-event → ``on_audit_event``.

    #5041 ①: see :class:`DisplayFrame`'s own docstring — same missing-
    attribution finding, same fix shape, same optional/defaulted field."""

    event: "Event"
    tag: FrameTag = FrameTag.EVENT
    agent: "str | None" = None


# A client consumes a stream of these; ``frame.tag`` selects the renderer entry.
Frame = "DisplayFrame | EventFrame"


@dataclass(frozen=True)
class BacklogBatch:
    """#5139 (architect ruling, issuecomment-5383272756): one reconnect/switch
    ``MESSAGES_SNAPSHOT`` burst, still bundled exactly as it arrived on the
    wire — ``AgUiTransport._consume_block`` decodes it as ONE SSE block, ONE
    list, and this is that fact carried forward instead of being flattened
    into individual :class:`DisplayFrame` items the way every other frame
    source is (the pre-#5139 shape, and the reason a remote client's history
    used to flow onto screen one row at a time instead of settling in with
    local restore's own single ``FlowModel.extend``/``insert_many`` reflow).

    Yielded through the SAME queue/:meth:`AgUiTransport.frames` stream every
    live :data:`Frame` flows through — deliberately NOT a side channel like
    :class:`~reyn.interfaces.transport.agui.protocol.StateUpdate` (routed to
    :class:`~reyn.interfaces.transport.agui.state.RemoteStatusView` instead
    of the frame stream). A side channel was this PR's OWN first draft and
    was reverted: it left the queue with nothing to put for a snapshot-only
    SSE block, so :func:`~reyn.interfaces.transport.drain.suspend_between_frames`
    never got a turn to run for one (measured — the reason a first connect
    with no further live activity never drained its own popped-but-unapplied
    backlog), and applying it synchronously at decode time would have let a
    live frame that arrived on the wire EARLIER but is dequeued LATER
    invert order against a backlog applied INSTANTLY off the decode thread.
    Putting it in the same queue makes wire-arrival-order and apply-order
    the SAME order, by construction, with nothing else to prove.

    ``agent``/``sid`` are the destination this batch is FOR — for a
    mid-stream session SWITCH (#3310 N3), the
    :class:`~reyn.interfaces.transport.frames.EventFrame` ``session_attached``
    announce always precedes the ``MESSAGES_SNAPSHOT`` re-fire it belongs
    with on the wire; the VERY FIRST connect's own batch has no such
    preceding announce (``AgUiEmitter.stream``'s initial reconnect chunks
    are sent before its ``session_attached``-bearing event loop even
    starts) and is seeded instead from what the caller already knows at
    connect time — see ``AgUiTransport.__init__``'s own comment. The
    consumer (``TextualChatApp._pump_frames``) compares this
    against ITS OWN current location right before applying — a mismatch
    means the connection has since moved on and this batch is stale, never
    "arrived late so still show it" (destination-based, not arrival-order-
    based — architect ruling, issuecomment-5383251430: "「在るか」は消失の
    witness になりません — 「どれが/いくつ」を訊く"). Not part of the
    :data:`Frame` union: only :class:`AgUiTransport` ever produces one and
    only ``_pump_frames`` interprets it — the generic renderer entry points
    (``.message`` / ``.on_audit_event``) never see it.

    #5139 C (architect ruling, issuecomment-5383993909): ``has_more`` /
    ``next_cursor`` carry the SAME server-side bound every OTHER reconnect
    backlog now respects (:data:`HYDRATE_PAGE_FRAMES`) — the server sends
    at most one page per request; ``has_more`` says whether an older page
    still exists, ``next_cursor`` is that older page's own request key (a
    turn's ``chain_id`` — the root id tool-call/result correlation, group
    parenting, and sticky state are all keyed on, never a message's own
    ``seq``, which a mid-turn cut would silently split). ``is_older_page``
    distinguishes this batch's OWN apply direction: ``False`` (the
    reconnect/switch snapshot, unchanged default) appends at the bottom;
    ``True`` (a client-driven ``ReachedTop`` pull, #5139 C) prepends at the
    top instead — see ``TextualChatApp._apply_backlog_batch``."""

    agent: str
    sid: str
    frames: "list[DisplayFrame]"
    has_more: bool = False
    next_cursor: "str | None" = None
    is_older_page: bool = False


#: The server sends at most this many frames per backlog page (reconnect
#: snapshot, switch re-fire, or an older-page pull) — #5139 C reuses the
#: SAME bound local restore's own lazy paging already uses
#: (``textual_chat/app.py``'s ``_HYDRATE_PAGE_FRAMES``) rather than
#: inventing a second number; both sides import this one constant so the
#: two can never drift apart.
HYDRATE_PAGE_FRAMES = 200


@dataclass(frozen=True)
class StatusApplied:
    """#5830 (architect FINAL ruling, same shape #5139 already established
    for :class:`BacklogBatch`): one decoded ``STATE_SNAPSHOT``/``STATE_DELTA``
    application, carried IN-STREAM — appended to the SAME list
    :meth:`~reyn.interfaces.transport.agui.client.AgUiTransport._consume_block`
    already builds for every other frame, not a side channel.

    Owner-hit this closes: web/connect's status bar (agent name / model /
    cost / ctx / session tree / …) stayed on the OLD agent right after
    ``/attach <other agent>``, updating only once the next turn's own
    frame arrived. Root cause (architect's own measurement): applying a
    decoded ``StateUpdate`` onto :class:`~reyn.interfaces.transport.agui.
    state.RemoteStatusView` (``AgUiTransport._consume_block``) never
    produced anything for :meth:`TextualChatApp._pump_frames` to see — the
    new values landed on the read-model instantly, but the ONE place that
    calls :meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.
    _refresh_live_chrome` (this pump's own per-frame trailer) had nothing
    to iterate for a snapshot/delta-only SSE block, so the redraw waited
    for whatever frame happened to arrive next.

    Carries no VALUES of its own — they are already on
    :class:`~reyn.interfaces.transport.agui.state.RemoteStatusView` by the
    time this item is even constructed (:meth:`_consume_block` applies the
    ``StateUpdate`` first, in the SAME branch, before appending this). Its
    only job is to exist as a stream item so ``_pump_frames``'s trailing
    ``_refresh_live_chrome()`` call fires in wire-arrival order — unlike
    :class:`BacklogBatch`, the pump does NOT ``continue`` past it.

    ``kind`` (#5886, architect ruling ①) is the ONE thing it does carry, and
    it exists because this class used to erase it. ``_consume_block``
    knows perfectly well which it decoded (``decoded.snapshot is not None``
    / ``decoded.delta is not None``) and handed the app the same opaque item
    for both — the information-losing point. The two are NOT
    interchangeable to a consumer:

    - ``"snapshot"`` is a HYDRATION point. It is the paired, server-side
      consistent view (#5179's ``_session_backlog_page_and_status``) of the
      queue at one instant, so it — and only it — may seed the sent-queue
      seq-gate's baseline.
    - ``"delta"`` is a display update. It moves the live status a viewer
      reads; it must never move the gate's baseline, because a delta can
      reach the client's read model long before the pump has processed the
      frames that baseline is supposed to be measured against (#5886's own
      measurement: ``AgUiTransport._pump_sse`` decodes without yielding
      between frames while ``frames()`` suspends between each, so the read
      model runs arbitrarily far ahead of the pump's position).

    A required field, not a defaulted one: a default would let a future
    producer silently pick the lenient answer — the shape #5818 spent a
    night removing elsewhere.

    ``snapshot`` (#5895, architect ruling on #5886's fix — the class was
    only narrowed, not closed, until this): a ``"snapshot"`` CARRIES the
    three values the sent-queue gate seeds from, captured at the instant
    the snapshot was taken. The seed reads THIS frame and nothing else —
    never the live read model. Reading the read model at seed time, even
    "at the snapshot frame's own position", is the #5886 defect with a
    narrower window: on the remote side ``_pump_sse`` can apply a later
    delta onto ``RemoteStatusView`` between enqueuing this frame and the
    pump reaching it; on the local side a submit can advance ``queue_seq``
    in the same gap. Both leave a baseline ahead of the frames it must
    admit. A value captured with the snapshot cannot move.

    Typed, not documented: a ``"snapshot"`` MUST carry one and a
    ``"delta"`` MUST NOT (``__post_init__``) — the read model stays the
    display's source, the frame is the seed's, and the two cannot be
    mixed by accident because the seed's parameter type is this class."""

    kind: Literal["snapshot", "delta"]
    snapshot: "QueueSnapshot | None" = None

    def __post_init__(self) -> None:
        if (self.kind == "snapshot") != (self.snapshot is not None):
            raise ValueError(
                f"StatusApplied(kind={self.kind!r}) must carry a QueueSnapshot "
                f"iff kind is 'snapshot' (got snapshot={self.snapshot!r})"
            )


@dataclass(frozen=True)
class QueueSnapshot:
    """The sent-queue gate's hydration values, as of one snapshot instant
    (#5895): the undispatched queue, whether a turn is active, and the
    monotonic ``queue_seq`` that becomes the gate's baseline. Produced by
    the same status builder the read model uses (``interfaces/repl/status.
    py``'s ``_snapshot_for_session``, via the remote ``project_status`` or
    the local ``_snapshot``), captured once, immutable."""

    queue: "tuple[dict, ...]"
    turn_active: bool
    queue_seq: int

    @classmethod
    def from_status(cls, values: "dict | None") -> "QueueSnapshot":
        """Build from a status dict in the ``_snapshot`` shape — the SAME
        three keys, read once, defensively typed (a wire dict may carry
        ``None`` where a fresh session has nothing)."""
        v = values or {}
        return cls(
            queue=tuple(dict(item) for item in (v.get("queue") or ())),
            turn_active=bool(v.get("turn_active", False)),
            queue_seq=int(v.get("queue_seq", 0) or 0),
        )


__all__ = [
    "BacklogBatch",
    "QueueSnapshot",
    "DisplayFrame",
    "EventFrame",
    "StatusApplied",
    "Frame",
    "FrameTag",
    "HYDRATE_PAGE_FRAMES",
    "forwarded_frame_kinds",
]
