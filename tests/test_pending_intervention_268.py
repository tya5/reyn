"""Tier 2: PendingIntervention origin-pin + stall + redirect
(issue #268 Phase 1, #270 umbrella First instance).

Pins the cross-channel intervention routing introduced by #268:

  - iv carries ``origin_channel_id`` (= "tui:..." / "a2a:..." / etc.).
  - ``Session.handle_intervention`` checks if the origin channel
    is still registered as a listener; if not, the iv is parked in
    the stalled queue instead of being delivered to a different
    channel.
  - Cross-channel operations: ``list_stalled_interventions`` (read),
    ``discard_pending_intervention`` (cancel future), and
    ``claim_pending_intervention`` (rebind origin + re-dispatch).
  - ``PendingOpView`` field shape is **pinned at Phase A landing** per
    tui-coder commitment so the TUI Pending tab + ``/pending`` slash
    command consume contract stays stable as future kinds (= MCP
    pending call, peer_delegate) land.

Pins:

  1. ``UserIntervention.origin_channel_id`` field default + to_dict /
     from_dict preservation.
  2. handle_intervention origin-pin check stalls when origin is
     unregistered; existing dispatch path runs when origin is
     registered (or ``origin_channel_id=None``).
  3. Stalled queue API: list_stalled / get_stalled / stalled_count /
     discard_stalled / claim_stalled all behave as documented.
  4. Session session-level operations dispatch to registry
     correctly + emit audit events.
  5. PendingOpView field shape is the documented set; from_intervention
     populates from iv correctly.
  6. Backwards-compat: iv with ``origin_channel_id=None`` follows the
     pre-#268 dispatch path unchanged.

No mocks. Real Session + real InterventionRegistry.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime

import pytest

from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.session import PendingOpView, Session
from reyn.user_intervention import (
    InterventionAnswer,
    UserIntervention,
)

# ── 1. UserIntervention origin_channel_id field ───────────────────────


def test_user_intervention_default_origin_channel_id_is_none() -> None:
    """Tier 2: backwards-compat — existing callers that don't pass
    ``origin_channel_id`` see ``None``, no stall routing applies.
    """
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    assert iv.origin_channel_id is None


def test_user_intervention_origin_channel_id_round_trips_through_dict() -> None:
    """Tier 2: to_dict + from_dict preserve origin_channel_id.

    Used by AgentSnapshot persistence (= issue #254 Phase 1 + #267
    Gap 5 Phase 1) so a restart can replay the iv's origin binding.
    """
    iv = UserIntervention(
        kind="ask_user",
        prompt="Q?",
        origin_channel_id="tui:session-abc",
    )
    restored = UserIntervention.from_dict(iv.to_dict())
    assert restored.origin_channel_id == "tui:session-abc"


def test_user_intervention_to_dict_omits_origin_when_none() -> None:
    """Tier 2: legacy ivs (= no origin) don't grow a new top-level key
    in their dict form, keeping snapshot files smaller + back-compatible.
    """
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    d = iv.to_dict()
    assert "origin_channel_id" not in d


# ── 2. InterventionRegistry stalled queue operations ──────────────────


def test_registry_mark_stalled_moves_iv_from_active_to_stalled() -> None:
    """Tier 2: ``mark_stalled(iv_id)`` moves an active iv to the
    stalled queue, removing it from active + order tracking.
    """
    async def _no_announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(
        on_announce=_no_announce, enforce_listener_presence=False,
    )

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    reg._active[iv.id] = iv
    reg._order.append(iv.id)

    assert reg.mark_stalled(iv.id) is True
    assert not reg.has_active(iv.id)
    assert not reg.is_queued(iv.id)
    assert reg.get_stalled(iv.id) is iv
    assert reg.stalled_count() == 1


def test_registry_mark_stalled_returns_false_when_iv_not_active() -> None:
    """Tier 2: mark_stalled is idempotent / safe — unknown iv_id
    returns False without raising.
    """
    async def _no_announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(on_announce=_no_announce)
    assert reg.mark_stalled("nonexistent") is False


def test_registry_list_stalled_returns_snapshot() -> None:
    """Tier 2: list_stalled returns a list (not a live reference) so
    iteration is safe across concurrent mutations.
    """
    async def _no_announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(on_announce=_no_announce)
    iv1 = UserIntervention(kind="ask_user", prompt="Q1?")
    iv2 = UserIntervention(kind="permission.shell", prompt="Run cmd?")
    # Seed via the public path: enqueue in _active then move to stalled.
    reg._active[iv1.id] = iv1
    reg._order.append(iv1.id)
    reg._active[iv2.id] = iv2
    reg._order.append(iv2.id)
    reg.mark_stalled(iv1.id)
    reg.mark_stalled(iv2.id)

    items = reg.list_stalled()
    # Both items are present in the snapshot.
    assert iv1 in items and iv2 in items
    # Mutating the registry via public discard doesn't affect the returned snapshot.
    reg.discard_stalled(iv1.id)
    assert iv1 in items and iv2 in items


def test_registry_discard_stalled_resolves_future_with_empty_answer() -> None:
    """Tier 2: discard_stalled resolves the iv's future with an empty
    InterventionAnswer + removes from the queue. Awaiters see this
    as a refusal (= existing cancellation contract).
    """
    async def _no_announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(on_announce=_no_announce)

    async def _drive() -> InterventionAnswer:
        iv = UserIntervention(kind="ask_user", prompt="Q?")
        reg._stalled[iv.id] = iv
        # Discard from another "channel".
        assert reg.discard_stalled(iv.id) is True
        assert not reg.has_stalled(iv.id)
        return await iv.future

    answer = asyncio.run(_drive())
    assert isinstance(answer, InterventionAnswer)
    assert answer.text == ""


def test_registry_claim_stalled_rebinds_origin_and_returns_iv() -> None:
    """Tier 2: claim_stalled removes from stalled queue, updates
    origin_channel_id, returns the iv so the caller can re-dispatch.
    """
    async def _no_announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(on_announce=_no_announce)
    iv = UserIntervention(
        kind="ask_user",
        prompt="Q?",
        origin_channel_id="tui:closed-session",
    )
    reg._stalled[iv.id] = iv

    claimed = reg.claim_stalled(iv.id, "tui:new-session")
    assert claimed is iv
    assert claimed.origin_channel_id == "tui:new-session"
    assert not reg.has_stalled(iv.id)


def test_registry_claim_stalled_returns_none_when_not_stalled() -> None:
    """Tier 2: claim_stalled on unknown id returns None — safe / no raise."""
    async def _no_announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(on_announce=_no_announce)
    assert reg.claim_stalled("nonexistent", "tui:session") is None


# ── 3. Session handle_intervention origin-pin routing ─────────────


def test_handle_intervention_with_no_origin_uses_existing_path() -> None:
    """Tier 2: backwards-compat — iv with origin_channel_id=None
    follows the pre-#268 dispatch path (= no stall check).

    With Phase 1 subscriber-guard (no listener registered) the
    dispatch short-circuits to empty answer; we verify the path
    reached the short-circuit rather than the stall queue.
    """
    session = Session(agent_name="test")
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    # No origin_channel_id, no listener.

    answer = asyncio.run(session.handle_intervention(iv))
    assert isinstance(answer, InterventionAnswer)
    assert answer.text == ""  # Phase 1 short-circuit
    # Did NOT land in stalled queue (= origin-pin doesn't trigger).
    assert not session.is_intervention_stalled(iv.id)


def test_handle_intervention_with_registered_origin_uses_dispatch_path() -> None:
    """Tier 2: when origin_channel_id is registered as a listener,
    the dispatch path runs (= origin alive → not stalled).
    """
    session = Session(agent_name="test")
    session.register_intervention_listener("tui:session-a")

    async def _drive() -> tuple[InterventionAnswer, str]:
        # iv created INSIDE the running loop so its future binds to
        # the same loop the registry awaits in (= mirrors the pattern
        # used elsewhere in #254 tests).
        iv = UserIntervention(
            kind="ask_user",
            prompt="Q?",
            origin_channel_id="tui:session-a",
        )
        task = asyncio.ensure_future(session.handle_intervention(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Resolve via deliver (= simulating tui input).
        await session._deliver_answer_to(iv, "answer-from-origin")
        result = await task
        return result, iv.id

    answer, iv_id = asyncio.run(_drive())
    assert answer.text == "answer-from-origin"
    # Not in stalled queue.
    assert not session.is_intervention_stalled(iv_id)


def test_handle_intervention_with_closed_origin_parks_in_stalled_queue() -> None:
    """Tier 2: when origin_channel_id is NOT in the listener set,
    handle_intervention parks the iv in stalled queue + awaits.

    Verified by:
      - iv lands in session._interventions._stalled
      - handle_intervention is awaiting (= doesn't return until
        future resolves via discard / claim)
    """
    session = Session(agent_name="test")
    # Register a different listener — origin won't match.
    session.register_intervention_listener("tui:current-session")

    async def _drive() -> InterventionAnswer:
        iv = UserIntervention(
            kind="ask_user",
            prompt="Q?",
            origin_channel_id="tui:closed-session",  # not registered
        )
        task = asyncio.ensure_future(session.handle_intervention(iv))
        # Yield twice so handle_intervention reaches the await on iv.future.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # iv must be in the stalled queue.
        assert session.is_intervention_stalled(iv.id)
        assert not task.done()
        # Discard to unblock the test.
        ok = await session.discard_pending_intervention(iv.id)
        assert ok is True
        return await task

    answer = asyncio.run(_drive())
    assert answer.text == ""  # Discarded → empty answer


def test_handle_intervention_emits_user_channel_stalled_route_event() -> None:
    """Tier 2: when origin-pin stalls the iv, the audit event
    ``intervention_routed{route="user_channel_stalled"}`` is emitted,
    distinct from the regular ``"user_channel"`` route event.
    """
    session = Session(agent_name="test")
    session.register_intervention_listener("tui:current")

    async def _drive() -> None:
        iv = UserIntervention(
            kind="ask_user",
            prompt="Q?",
            origin_channel_id="tui:closed",
        )
        task = asyncio.ensure_future(session.handle_intervention(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await session.discard_pending_intervention(iv.id)
        await task

    asyncio.run(_drive())

    routed_events = [
        e for e in session._chat_events.to_json()
        if e.get("type") == "intervention_routed"
    ]
    assert routed_events
    last = routed_events[-1]["data"]
    assert last["route"] == "user_channel_stalled"
    assert last["origin_channel_id"] == "tui:closed"


# ── 4. Session cross-channel operations ───────────────────────────


def test_list_stalled_interventions_returns_pending_op_views() -> None:
    """Tier 2: list_stalled_interventions returns a list of
    PendingOpView with the documented field shape.
    """
    session = Session(agent_name="test")
    session.register_intervention_listener("tui:current")

    async def _drive() -> list[PendingOpView]:
        iv = UserIntervention(
            kind="ask_user",
            prompt="What's your name?",
            detail="for greeting",
            origin_channel_id="tui:closed",
        )
        task = asyncio.ensure_future(session.handle_intervention(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        views = session.list_stalled_interventions()
        # Clean up.
        await session.discard_pending_intervention(iv.id)
        await task
        return views

    views = asyncio.run(_drive())
    assert views, "expected at least one stalled view"
    v = next(v for v in views if v.origin_channel_id == "tui:closed")
    assert isinstance(v, PendingOpView)
    assert v.kind == "intervention"
    assert v.origin_channel_id == "tui:closed"
    assert v.summary == "What's your name?"
    assert v.detail == "for greeting"


def test_discard_pending_intervention_emits_audit_event_on_success() -> None:
    """Tier 2: discard_pending_intervention emits
    ``pending_intervention_discarded`` audit event for the P6 audit
    trail when the iv was actually discarded.
    """
    session = Session(agent_name="test")
    session.register_intervention_listener("tui:current")

    async def _drive() -> None:
        iv = UserIntervention(
            kind="ask_user",
            prompt="Q?",
            origin_channel_id="tui:closed",
        )
        task = asyncio.ensure_future(session.handle_intervention(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ok = await session.discard_pending_intervention(
            iv.id, reason="test_explicit_reason",
        )
        assert ok is True
        await task

    asyncio.run(_drive())

    discarded = [
        e for e in session._chat_events.to_json()
        if e.get("type") == "pending_intervention_discarded"
    ]
    assert discarded
    assert discarded[-1]["data"]["reason"] == "test_explicit_reason"


def test_discard_pending_intervention_returns_false_for_unknown_id() -> None:
    """Tier 2: discard on unknown id is safe + returns False without
    raising or emitting an event.
    """
    session = Session(agent_name="test")

    async def _drive() -> bool:
        return await session.discard_pending_intervention("nonexistent")

    ok = asyncio.run(_drive())
    assert ok is False


def test_claim_pending_intervention_rebinds_origin_and_returns_view() -> None:
    """Tier 2: claim updates origin_channel_id to caller's channel +
    returns the PendingOpView reflecting the new state.

    Lifecycle:
      - stalled iv parked with origin "tui:closed"
      - claim called from "tui:claimer" → rebind + returns view
      - claim spawns a background _dispatch_intervention for the
        rebound iv; we deliver an answer via the new channel to
        complete the lifecycle cleanly
    """
    session = Session(agent_name="test")
    session.register_intervention_listener("tui:current")
    session.register_intervention_listener("tui:claimer")

    async def _drive() -> "PendingOpView | None":
        iv = UserIntervention(
            kind="ask_user",
            prompt="Q?",
            origin_channel_id="tui:closed",
        )
        task = asyncio.ensure_future(session.handle_intervention(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Sanity: iv is in stalled queue.
        assert session.is_intervention_stalled(iv.id)
        # Claim from a different channel.
        view = await session.claim_pending_intervention(
            iv.id, "tui:claimer",
        )
        # claim removed iv from _stalled + scheduled a re-dispatch
        # task. Resolve via deliver_answer so the awaited future
        # resolves and both tasks complete.
        await asyncio.sleep(0)
        await session._deliver_answer_to(iv, "from-claimer")
        await task
        return view

    view = asyncio.run(_drive())
    assert view is not None
    assert view.origin_channel_id == "tui:claimer"


def test_claim_pending_intervention_returns_none_for_unknown_id() -> None:
    """Tier 2: claim on unknown id returns None, no exception."""
    session = Session(agent_name="test")

    async def _drive() -> "PendingOpView | None":
        return await session.claim_pending_intervention(
            "nonexistent", "tui:claimer",
        )

    result = asyncio.run(_drive())
    assert result is None


# ── 5. PendingOpView shape pin (= tui-coder commitment) ───────────────


def test_pending_op_view_has_documented_field_shape() -> None:
    """Tier 2: PendingOpView carries exactly the documented fields.

    Per tui-coder's #270 framework commitment, the field shape is
    pinned at Phase A landing so the TUI Pending tab + ``/pending``
    slash command code path doesn't churn as new kinds land.
    """
    # All required + optional fields present + type-checkable.
    fields = inspect.signature(PendingOpView).parameters
    expected = {"id", "kind", "origin_channel_id", "created_at", "summary", "detail"}
    assert set(fields.keys()) == expected, (
        f"PendingOpView fields drifted from the Phase A commitment: "
        f"got {set(fields.keys())}, expected {expected}"
    )


def test_pending_op_view_from_intervention_populates_all_fields() -> None:
    """Tier 2: from_intervention populates every documented field
    from the source iv.
    """
    iv = UserIntervention(
        kind="ask_user",
        prompt="What's your name?",
        detail="for greeting",
        origin_channel_id="tui:abc",
    )
    view = PendingOpView.from_intervention(iv)
    assert view.id == iv.id
    assert view.kind == "intervention"
    assert view.origin_channel_id == "tui:abc"
    # created_at is ISO-formatted (= parseable).
    assert datetime.fromisoformat(view.created_at)
    assert view.summary == "What's your name?"
    assert view.detail == "for greeting"


def test_pending_op_view_is_immutable() -> None:
    """Tier 2: PendingOpView is frozen — callers cannot mutate the
    view, ensuring TUI / slash callers don't accidentally corrupt
    each other's references.
    """
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    view = PendingOpView.from_intervention(iv)
    with pytest.raises(Exception):
        view.summary = "changed"  # type: ignore[misc]


def test_pending_op_view_handles_iv_without_origin_channel() -> None:
    """Tier 2: from_intervention tolerates ``origin_channel_id=None``
    by rendering the field as empty string (= clean display in TUI).
    """
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    view = PendingOpView.from_intervention(iv)
    assert view.origin_channel_id == ""
