"""Tier 2: OS invariant — #3408 identity-bound hot-list sink E2E.

#3408 replaced ``Session._build_retrieval_bundle``'s ``_on_hot_list_changed``
closure's NAME lookup (``self._chat_events``, resolved at call time) with an
IDENTITY binding (the ``chat_events`` builder arg, captured once). This is
the E2E identity witness the architect's firm required: through a REAL
``Session`` (never a mock/stub of ``ActionUsageTracker`` or ``EventLog``),
recording a real action usage -> ``merge_compacted`` reordering the compacted
ranking -> the sink firing -> ``session._chat_events.emit("hot_list_updated",
...)`` -> a real subscriber attached to ``session._chat_events`` AFTER
construction receiving that event.

The point this proves: the sink the retrieval builder wired at construction
time and the ``EventLog`` a caller reaches via ``session._chat_events`` after
construction are THE SAME OBJECT. A name-resolution bug (#2856's class) would
silently emit onto some OTHER EventLog no subscriber here is watching, and
this test would see the outbox stay empty instead.
"""
from __future__ import annotations

import time

from reyn.config.embedding import ActionRetrievalConfig
from reyn.core.events.events import Event
from tests._support.agent_session import make_session


class _EventSink:
    """A real (non-mock) chat-event subscriber — a plain callback collector
    (mirrors tests/test_agent_delta_chat_event_3288.py's ``_EventSink``)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)


def test_hot_list_sink_reaches_the_sessions_own_chat_events() -> None:
    """Tier 2: a real ActionUsageTracker ranking change reaches a subscriber
    attached to session._chat_events — proving the sink the retrieval
    builder wired IS the session's live EventLog, bound by identity rather
    than rediscovered by name."""
    session = make_session(
        agent_name="test_agent_3408",
        action_retrieval_config=ActionRetrievalConfig(hot_list_n=5),
    )
    tracker = session._action_usage_tracker
    assert tracker is not None, (
        "hot_list_n=5 must construct a real ActionUsageTracker — if this is "
        "None, _build_retrieval_bundle's conditional changed shape"
    )

    sink = _EventSink()
    session._chat_events.add_subscriber(sink)

    # A synthetic-but-validly-shaped (<category>__<entry>, category in
    # CATEGORIES) usage record -- deliberately NOT a real production action
    # name, so this fixture never reads as "here is a currently-valid
    # action" -- reordering the compacted ranking from empty -> one entry
    # (order always changes on the first record), which fires
    # ActionUsageTracker's on_ranking_changed callback for real.
    tracker.merge_compacted([("file__test_hot_list_entry", time.time())])

    hot_list_events = [e for e in sink.events if e.type == "hot_list_updated"]
    assert hot_list_events, (
        "the identity-bound hot-list sink did not reach session._chat_events' "
        "own subscriber — either the closure is emitting onto a DIFFERENT "
        "EventLog than the one session._chat_events exposes (the #2856 "
        f"failure class), or the ranking-changed path didn't fire. Captured: {sink.events}"
    )
    assert hot_list_events[-1].data.get("ranking") == tracker.full_ranking()
