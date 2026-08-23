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
from typing import TYPE_CHECKING

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
    """A display-path frame: one verbatim outbox message → ``renderer.message``."""

    message: "OutboxMessage"
    tag: FrameTag = FrameTag.DISPLAY


@dataclass(frozen=True)
class EventFrame:
    """An event-path frame: one renderer-relevant audit-event → ``on_audit_event``."""

    event: "Event"
    tag: FrameTag = FrameTag.EVENT


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


__all__ = [
    "BacklogBatch",
    "DisplayFrame",
    "EventFrame",
    "Frame",
    "FrameTag",
    "HYDRATE_PAGE_FRAMES",
    "forwarded_frame_kinds",
]
