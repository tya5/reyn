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

from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
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


# ── model_cost_block (#1867 / FP-0052 S4) ────────────────────────────────────


def test_model_cost_block_declined_emits_marker() -> None:
    """Tier 2: model_cost_block with reason=declined → [✗ model switch declined:] marker.

    Without this handler, a user who says No to the high-cost confirm gets no
    feedback — the model chip stays unchanged but nothing explains why.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="model_cost_block",
        data={"model": "gpt-4o", "model_class": "gpt4o", "reason": "declined"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "model switch declined" in only.text
    assert "gpt-4o" in only.text


def test_model_cost_block_approved_emits_nothing() -> None:
    """Tier 2: model_cost_block with reason=approved → no outbox message.

    The status-bar chip updates to the new model; no extra marker is needed.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="model_cost_block",
        data={"model": "gpt-4o", "reason": "approved"},
    ))
    assert _drain(q) == []


def test_model_cost_block_non_interactive_emits_nothing() -> None:
    """Tier 2: model_cost_block with reason=non_interactive_fail_closed → no message.

    No human present; the operator discovers the block via the calling exception.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="model_cost_block",
        data={"model": "gpt-4o", "reason": "non_interactive_fail_closed"},
    ))
    assert _drain(q) == []


def test_model_cost_block_missing_reason_emits_nothing() -> None:
    """Tier 2: model_cost_block with no reason field → no message (forward-compat).

    Future event-shape additions must not accidentally trigger the declined marker.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="model_cost_block", data={"model": "gpt-4o"}))
    assert _drain(q) == []


# ── config hot-reload (#2073) ─────────────────────────────────────────────────


def test_config_reloaded_with_components_emits_marker() -> None:
    """Tier 2: config_reloaded with changed components → [↻ config reloaded: <names>] marker.

    Without this handler, a user who ran /reload gets no confirmation that the
    reload completed or which components changed — only the /reload "scheduled"
    message from earlier in the turn.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reloaded",
        data={"components": ["hooks", "mcp"], "failed": [], "source": "operator"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "config reloaded" in only.text
    assert "hooks" in only.text
    assert "mcp" in only.text


def test_config_reloaded_with_no_changes_emits_nothing() -> None:
    """Tier 2: config_reloaded with empty components+failed → no outbox marker.

    A reload that touched nothing is already confirmed by the /reload reply;
    a redundant "nothing changed" marker would be noise.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reloaded",
        data={"components": [], "failed": [], "source": "operator"},
    ))
    assert _drain(q) == []


def test_config_reloaded_with_failed_seams_includes_failure_note() -> None:
    """Tier 2: config_reloaded with failed seams → marker includes failure names.

    A seam failure is otherwise silently logged; surfacing it in the conv pane
    lets the user know the reload was partial.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reloaded",
        data={"components": ["hooks"], "failed": ["cron"], "source": "operator"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert "config reloaded" in only.text
    assert "hooks" in only.text
    assert "cron" in only.text
    assert "failed" in only.text


def test_config_reload_rejected_emits_error_marker() -> None:
    """Tier 2: config_reload_rejected → [✗ config reload rejected: <reason>] marker.

    Without this event the user sees the /reload "scheduled" confirmation but
    then nothing when the validate-before-apply step rejects the malformed
    IN-set — the next turn silently runs under the old config.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reload_rejected",
        data={"reason": "cron.jobs must be a list", "source": "operator"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "config reload rejected" in only.text
    assert "cron.jobs must be a list" in only.text


def test_config_reload_rejected_missing_reason_uses_fallback() -> None:
    """Tier 2: config_reload_rejected with no reason field → generic fallback text."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="config_reload_rejected", data={}))
    msgs = _drain(q)
    (only,) = msgs
    assert "config reload rejected" in only.text
    assert "malformed config" in only.text
