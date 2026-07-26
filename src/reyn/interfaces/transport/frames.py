"""Tagged frame vocabulary for the :mod:`reyn.interfaces.transport` client seam.

The inline CUI historically consumed its session through **two independent
source paths** (ADR-0039 P1): the display outbox (``session.outbox`` → the
registry forwarder → ``repl_outbox`` → ``renderer.message``) and the
chat-event subscription (``session.chat_events`` → ``renderer.on_chat_event``,
which drives the Working / Running / Waiting-for-you indicator). A remote
client, however, sees ONE ordered event stream (AG-UI / SSE, P2). This module
defines the unified, tagged frame vocabulary that both the local
``InProcessTransport`` and any future wire transport present to the client:

- :class:`DisplayFrame` wraps a verbatim :class:`~reyn.runtime.outbox.OutboxMessage`
  (the display path).
- :class:`EventFrame` wraps the renderer-relevant *subset* of chat-events (the
  working-indicator path).

A frame carries its :class:`FrameTag` so the consuming client dispatches to the
renderer's two entry points (``message`` for display, ``on_chat_event`` for
event) at the consuming end — one stream in, two renderer entry points out.

The forward-set (:func:`renderer_chat_events`) is mostly **DERIVED** from the
renderer's own vocabulary — ``_WAITING_ON_BY_EVENT`` (the tool-axis table) plus
the turn / intervention-answer events ``on_chat_event`` handles — never
hand-listed. The dual-stream completeness gate
(``tests/test_transport_dual_stream_completeness.py``) binds the transport's
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
    EVENT = "event"      # → renderer.on_chat_event(Event)


# The turn-lifecycle + intervention-answer chat-events the renderer's
# ``on_chat_event`` consumes DIRECTLY (i.e. not via the ``_WAITING_ON_BY_EVENT``
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
        # #3300 P3 (Y-server): cancel-by-id for an UNDISPATCHED (queued) user
        # message — the server-authoritative removal signal (never a
        # client-local "cancel succeeded" response) a client's sent-queue
        # rendering applies, exclusive with `turn_started` for the same
        # msg_id (owner addendum §6a: an item leaves the sent queue via
        # exactly one of these two deltas). Carries `msg_id` + `seq` (the
        # same order-race-gate token) — see ``Session.cancel_queued``.
        "inbox_cancel",
    }
)


# #3288 ③b: streamed LLM content-delta chat-events — the owner-ratified L4
# replacement (issue #3288 comment thread): a partial rides a chat-event
# (never an ``OutboxMessage`` kind, which the closed display vocabulary would
# have to register — the category error the owner's decision designs out).
# UNLIKE :data:`_TURN_AND_ANSWER_EVENTS` above, this was forwarded AHEAD OF
# any consumer — ③c has since added the textual_chat coalescing handler
# (``TextualChatApp._handle_agent_delta_event``), but the plain/repl renderer
# still branches on nothing for it (and may never). This is legal per the
# dual-stream completeness gate's actual direction (``tests/test_transport_dual_stream_completeness.py``:
# ``consumed ⊆ forwarded``, never the reverse) — a forwarded event nobody
# consumes yet is not a coverage gap, and a surface with no handler for an
# EVENT frame consumes-but-drops it (never renders it), unlike an unknown
# DISPLAY kind (which a presenter renders generically) — see the ③b PR body
# for the frame-level witness of that "no visible-garbage window" property.
_STREAMING_EVENTS = frozenset({"agent_delta"})


@lru_cache(maxsize=1)
def renderer_chat_events() -> frozenset[str]:
    """The set of chat-event types the transport forwards onto the unified
    frame stream (both ``InProcessTransport`` and the AG-UI endpoint filter
    against this).

    Union of:

    - ``_WAITING_ON_BY_EVENT.keys()`` (``interfaces/repl/status.py``) — the
      tool-axis WaitingOn transition table (``tool_called`` / ``tool_returned``
      / ``tool_failed``); extending WaitingOn to a new axis is one new entry
      there and this set follows automatically.
    - :data:`_TURN_AND_ANSWER_EVENTS` — the turn-lifecycle / intervention-answer
      / user-submitted events ``renderer.on_chat_event`` branches on directly
      (DERIVED from the renderer's own vocabulary, never hand-listed for this
      half — see ``tests/test_transport_dual_stream_completeness.py``).
    - :data:`_STREAMING_EVENTS` (#3288 ③b) — the ONE deliberate exception to
      "derived, not hand-listed": forwarded ahead of any consumer in THIS
      (plain/repl) renderer, which still has no ``agent_delta`` branch and
      may never — an unconsuming surface silently drops it (opt-in draw)
      rather than it vanishing on the wire. ③c has since added the actual
      consumer in a DIFFERENT surface (``TextualChatApp._handle_agent_delta_event``,
      ``interfaces/inline/textual_chat/app.py``), proving the forward-ahead
      design worked: the consumer landed with zero changes needed here.
    """
    return frozenset(_WAITING_ON_BY_EVENT.keys()) | _TURN_AND_ANSWER_EVENTS | _STREAMING_EVENTS


@dataclass(frozen=True)
class DisplayFrame:
    """A display-path frame: one verbatim outbox message → ``renderer.message``."""

    message: "OutboxMessage"
    tag: FrameTag = FrameTag.DISPLAY


@dataclass(frozen=True)
class EventFrame:
    """An event-path frame: one renderer-relevant chat-event → ``on_chat_event``."""

    event: "Event"
    tag: FrameTag = FrameTag.EVENT


# A client consumes a stream of these; ``frame.tag`` selects the renderer entry.
Frame = "DisplayFrame | EventFrame"


__all__ = [
    "DisplayFrame",
    "EventFrame",
    "Frame",
    "FrameTag",
    "renderer_chat_events",
]
