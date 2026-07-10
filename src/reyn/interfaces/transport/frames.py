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

The forward-set (:func:`renderer_chat_events`) is **DERIVED** from the
renderer's own vocabulary — ``_WAITING_ON_BY_EVENT`` (the tool-axis table) plus
the turn / intervention-answer events ``on_chat_event`` handles — never
hand-listed. The dual-stream completeness gate
(``tests/test_transport_dual_stream_completeness.py``) binds the transport's
coverage to that vocabulary so a renderer event the transport does not forward
fails CI instead of silently vanishing on the wire (the A2 dual-stream bug,
designed out).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING

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
    }
)


@lru_cache(maxsize=1)
def renderer_chat_events() -> frozenset[str]:
    """The exact set of chat-event types the renderer consumes — the transport's
    forward-set, DERIVED from the renderer's vocabulary, never hand-listed.

    Union of:

    - ``_WAITING_ON_BY_EVENT.keys()`` (``interfaces/inline/app.py``) — the
      tool-axis WaitingOn transition table (``tool_called`` / ``tool_returned``
      / ``tool_failed``); extending WaitingOn to a new axis is one new entry
      there and this set follows automatically.
    - :data:`_TURN_AND_ANSWER_EVENTS` — the turn-lifecycle + intervention-answer
      events ``renderer.on_chat_event`` branches on directly.

    Deferred import avoids a module-load cycle (``app`` imports the renderer).
    """
    from reyn.interfaces.inline.app import _WAITING_ON_BY_EVENT

    return frozenset(_WAITING_ON_BY_EVENT.keys()) | _TURN_AND_ANSWER_EVENTS


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
