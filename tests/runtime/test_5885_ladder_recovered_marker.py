"""Tier 2: #5885 (architect ruling 3) — the shrink ladder's completion gets
a conversation-face marker.

The in-turn ladder calls the engine's ``compact()`` directly, so it emits
``compaction_started`` (``[⟳ compacting …]``) but never ``compaction_
completed`` — that one's only producer is ``CompactionController``. Its
success is ``recovery_summary_persisted{outcome="persisted"}``, which no
forwarder handler consumed: the episode showed a start row and then nothing
("compact できてたのか不明", owner). One row per persisted fold, carrying
``compaction_episode_marker`` so the local TUI absorbs it into the open
episode entry while a remote / generic client shows the line as-is.

Same shape as ``test_4380_lifecycle_forwarder_new_markers.py``: a real
``ChatLifecycleForwarder`` over a real queue, called with a real ``Event``.
"""
from __future__ import annotations

import asyncio

from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event


def _drain(q: "asyncio.Queue") -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _run(event_type: str, data: dict) -> list:
    q: "asyncio.Queue" = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type=event_type, data=data))
    return _drain(q)


def test_a_persisted_fold_draws_one_recovered_marker_with_the_seq() -> None:
    """Tier 2: the accept side — one system row naming the fold's seq,
    carrying the episode-marker meta the TUI absorbs on."""
    (msg,) = _run("recovery_summary_persisted", {"outcome": "persisted", "covers_through_seq": 42})
    assert msg.kind == "system"
    assert "recovered" in msg.text and "42" in msg.text, msg.text
    assert msg.meta.get("compaction_episode_marker") is True, msg.meta


def test_the_other_outcomes_draw_nothing() -> None:
    """Tier 2: ``no_covers_through_seq`` / ``already_covered`` are no-ops
    on the flow — nothing was folded, nothing to announce."""
    for outcome in ("no_covers_through_seq", "already_covered"):
        assert _run("recovery_summary_persisted", {"outcome": outcome, "covers_through_seq": 42}) == [], outcome


def test_a_persisted_fold_without_a_seq_still_draws_a_generic_marker() -> None:
    """Tier 2: forward-compat with an event shape lacking the seq — a
    generic marker, never a fabricated number."""
    (msg,) = _run("recovery_summary_persisted", {"outcome": "persisted"})
    assert "recovered" in msg.text and "seq" not in msg.text, msg.text
