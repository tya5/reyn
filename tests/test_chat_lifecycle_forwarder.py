"""Tier 2: ChatLifecycleForwarder bridges session-level events → outbox (issue #162).

When ``CompactionController`` finishes collapsing N early-session turns
into a rolling summary, the conv pane previously showed nothing — users
had no signal that early turns had been replaced. This forwarder is a
session-scoped sibling of ``ChatEventForwarder`` (= per-skill) that
pushes a ``[↑ N turns compacted]`` system marker into the outbox so the
conversation pane's ``_render_system_message`` path can display it.

Pins:
  1. ``compaction_completed`` event → ``OutboxMessage(kind="system",
     text="[↑ N turns compacted]")``.
  2. Pluralisation: ``N=1`` → "1 turn", ``N>1`` → "N turns".
  3. Missing ``new_turn_count`` falls back to a generic marker (=
     forward-compat with event-shape variation).
  4. Unrelated event types are dropped (= no spurious outbox writes).
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.chat.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_compaction_completed_emits_system_marker() -> None:
    """Tier 2: compaction_completed with new_turn_count writes [↑ N turns compacted]."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_completed",
        data={"new_turn_count": 8, "covers_through_seq": 42},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[↑ 8 turns compacted]"


def test_compaction_singular_turn_uses_singular_label() -> None:
    """Tier 2: pluralisation — 1 turn is singular."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_completed",
        data={"new_turn_count": 1, "covers_through_seq": 5},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ 1 turn compacted]"


def test_compaction_missing_count_uses_generic_marker() -> None:
    """Tier 2: forward-compat fallback when new_turn_count is absent.

    Future event-shape variations (= compaction subtypes that don't
    expose a turn count) still surface a marker rather than silently
    dropping the signal.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_completed", data={}))
    msgs = _drain(q)
    (only,) = msgs
    assert only.text == "[↑ history compacted]"


def test_compaction_zero_count_uses_generic_marker() -> None:
    """Tier 2: a 0-count event is treated as missing (= no useful marker).

    Prevents spurious "[↑ 0 turns compacted]" if a future emit site
    fires with new_turn_count=0.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_completed", data={"new_turn_count": 0}))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ history compacted]"


def test_unrelated_event_is_dropped() -> None:
    """Tier 2: events with no matching on_<type> handler don't write to outbox.

    Lifecycle forwarder shares the EventLog subscriber slot with the
    session's per-skill chat events — it must NOT echo phase / llm /
    skill events into the outbox (those are the per-skill forwarder's
    job).
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="phase_started", data={"phase": "resolve"}))
    fwd(Event(type="llm_called", data={"model": "gemini-2.5-flash-lite"}))
    fwd(Event(type="user_message_received", data={"text": "hi"}))
    assert _drain(q) == []


def test_compaction_started_is_not_surfaced() -> None:
    """Tier 2: compaction_started doesn't emit a marker (= only completed does).

    A compaction may abort mid-run; surfacing the marker on completion
    only guarantees the user signal corresponds to a real summary
    landing in history.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_started", data={"new_turn_count": 8}))
    assert _drain(q) == []


# ── budget_warn (wave-5 C5) ──────────────────────────────────────────


def test_budget_warn_emits_lifecycle_marker_with_pct() -> None:
    """Tier 2: budget_warn → ``[↑ budget warn: <dim> (N%)]`` lifecycle marker.

    Without this forwarding path, ``budget_warn`` events only showed up
    in the Events tab (= side panel, default-closed). A user with the
    panel closed had no in-conv signal that the daily cap was being
    approached.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="budget_warn",
        data={
            "dimension": "daily_tokens",
            "agent": "default",
            "chain_id": "abc123",
            "current": 80000,
            "hard": 100000,
        },
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[↑ budget warn: daily_tokens (80%)]"


def test_budget_warn_without_numeric_context_drops_pct() -> None:
    """Tier 2: missing / non-numeric current / hard → no ``(N%)`` annotation.

    The marker still surfaces — pct just degrades to "no annotation"
    rather than failing the whole emit.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="budget_warn",
        data={"dimension": "rate_limit"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.text == "[↑ budget warn: rate_limit]"


def test_budget_warn_missing_dimension_uses_generic_label() -> None:
    """Tier 2: absent ``dimension`` falls back to the generic ``budget`` label."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="budget_warn", data={}))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ budget warn: budget]"


def test_budget_warn_zero_hard_drops_pct_safely() -> None:
    """Tier 2: ``hard=0`` would divide by zero — pct degrades, no crash."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="budget_warn",
        data={"dimension": "daily_tokens", "current": 100, "hard": 0},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ budget warn: daily_tokens]"
