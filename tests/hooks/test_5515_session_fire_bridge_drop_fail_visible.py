"""Tier 2: #5515 (2nd of 2 sites) — ``_SessionFireBridge.submit``'s own
queue-full drop is now fail-visible, applying the SAME shape
``tests/hooks/test_5515_ingress_bridge_drop_fail_visible.py`` already
proved for ``_BoundedEventBridge`` (#5515 PR1, landed) to this sibling
out-of-process bridge (``cron_fired``/``webhook_received``). Both fire the
SAME shared ``ingress_bridge_dropped`` P6 audit-event, keyed apart by
``source`` — see ``docs/reference/runtime/events.md``'s "External events"
section for the full 3-row table.

Unlike PR1's file (which drove ``_BoundedEventBridge``/its adapters
directly), this file drives the REAL PUBLIC production entry point,
``reyn.hooks.external_fire.fire_and_forget``, against a real ``Session``
(``tests/_support/agent_session.make_session`` — no mocks), and observes
the audit-event via ``session.subscribe_audit_events`` — the same real
consumer seam ``test_2761_pr2_hotreload_immediate_apply.py`` uses for
``bus_subscriber_dropped``, and the one this whole issue's own read-mouth
check (#5515 PR1's own PR body) rests on. ``EventLog.emit`` defers to an
async dispatch queue whenever a loop is running (#4966), so every test
below reads ``received`` only after ``await settle(session)`` —
``tests/_support/events.settle``, the same wrapper
``test_2761_pr2_hotreload_immediate_apply.py`` uses.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks import external_fire
from reyn.hooks.external_fire import _AUDIT_EVERY_N_DROPS
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import settle


def _make_session(tmp_path: Path) -> Session:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return make_session(agent_name="fire-bridge-drop-agent", state_log=state_log)


@pytest.mark.asyncio
async def test_queue_full_drop_is_fail_visible(tmp_path: Path):
    """Tier 2: overflowing the bridge's bounded queue increments
    ``external_fire.dropped_dispatch_count(session)`` and fires a
    metadata-only ``ingress_bridge_dropped`` P6 audit-event on the FIRST
    drop, reaching a subscriber registered via
    ``session.subscribe_audit_events`` — ``source``/``point``/``drop_count``
    only, never the fired point's own ``template_vars``.

    No ``await`` between :func:`fire_and_forget` calls, so the lazily-
    started drain task never gets a chance to run before the queue
    overflows (same non-interleaving argument PR1's / #2620's own suite
    relies on).

    Strip-falsify: remove the ``self._audit_drop(point)`` call in
    ``_SessionFireBridge.submit`` (or the ``self._drop_count += 1`` line)
    and this test goes RED — no ``ingress_bridge_dropped`` reaches
    ``received`` (performed during review: the ``(only,)`` unpack below
    raises ``ValueError`` over the empty result, not silently pass)."""
    session = _make_session(tmp_path)
    received: list[tuple[str, dict]] = []
    session.subscribe_audit_events(lambda e: received.append((e.type, dict(e.data))))

    maxsize = 1
    external_fire.fire_and_forget(session, "webhook_received", {"sender": "a"}, maxsize=maxsize)
    # Queue now holds 1 (its maxsize) undrained fire; the NEXT overflows it
    # (drop-newest — the new fire itself is dropped).
    external_fire.fire_and_forget(session, "webhook_received", {"sender": "b"}, maxsize=maxsize)

    assert external_fire.dropped_dispatch_count(session) == 1

    await settle(session)
    dropped = [d for (kind, d) in received if kind == "ingress_bridge_dropped"]
    (only,) = dropped  # exactly one audit-event fired — unpack-must-flip
    assert only["source"] == "_SessionFireBridge"
    assert only["point"] == "webhook_received"
    assert only["drop_count"] == 1
    # never the dropped fire's own template_vars
    assert "sender" not in only and "template_vars" not in only


@pytest.mark.asyncio
async def test_sustained_overflow_audits_first_then_every_nth_not_every_drop(tmp_path: Path):
    """Tier 2: under sustained overflow the audit-event fires on the first
    drop and then only every Nth drop — not once per drop.
    ``dropped_dispatch_count`` still counts every single drop regardless of
    audit cadence (mirrors PR1's own identical proof for
    ``_BoundedEventBridge``)."""
    session = _make_session(tmp_path)
    received: list[tuple[str, dict]] = []
    session.subscribe_audit_events(lambda e: received.append((e.type, dict(e.data))))

    maxsize = 1
    total_fires = _AUDIT_EVERY_N_DROPS + 2  # 1 fills the queue, the rest all drop
    for i in range(total_fires):
        external_fire.fire_and_forget(
            session, "cron_fired", {"i": i}, maxsize=maxsize,
        )

    expected_drops = total_fires - 1
    assert external_fire.dropped_dispatch_count(session) == expected_drops

    await settle(session)
    dropped = [d for (kind, d) in received if kind == "ingress_bridge_dropped"]
    # first drop (drop_count == 1) + the Nth drop (drop_count == _AUDIT_EVERY_N_DROPS) —
    # exactly two audit-events fired, unpack-must-flip if the cadence regresses.
    (first, nth) = dropped
    assert first["drop_count"] == 1
    assert nth["drop_count"] == _AUDIT_EVERY_N_DROPS


@pytest.mark.asyncio
async def test_source_distinguishes_this_bridge_from_the_ingress_bridge(tmp_path: Path):
    """Tier 2: lead-coder review requirement carried over from PR1 (#5515)
    — since ``ingress_bridge_dropped`` is a SHARED kind, ``source`` must
    distinguish THIS bridge's drops from ``_BoundedEventBridge``'s. Drives
    the real ``_SessionFireBridge`` (via ``fire_and_forget``) AND a real
    ``McpIngressAdapter`` (via ``reyn.hooks.ingress``) against the SAME
    session's audit stream, proving the two never collide — the identical
    non-collision assertion (``!=``, never equality to a literal) PR1's own
    ``test_source_distinguishes_which_adapters_bridge_dropped`` uses, for
    the same reason: a future rename of either source must not flip this
    red.

    Strip-falsify (performed during review): with ``_SessionFireBridge.
    submit``'s own ``self._audit_drop(point)`` call removed, this test (and
    the two above) go RED — the ``(fire_call,)`` unpack raises
    ``ValueError`` over an empty result (only the ingress-side drop still
    fires; ``_SessionFireBridge``'s own drop is silent again, reproducing
    exactly the pre-#5515 gap this PR closes)."""
    from reyn.hooks.event import HookEvent
    from reyn.hooks.ingress import McpIngressAdapter

    session = _make_session(tmp_path)
    received: list[tuple[str, dict]] = []
    session.subscribe_audit_events(lambda e: received.append((e.type, dict(e.data))))

    external_fire.fire_and_forget(session, "webhook_received", {}, maxsize=1)
    external_fire.fire_and_forget(session, "webhook_received", {}, maxsize=1)
    await settle(session)
    (fire_call,) = [d for (kind, d) in received if kind == "ingress_bridge_dropped"]
    received.clear()

    async def _never_drains(*_args, **_kwargs) -> None:
        raise AssertionError("drain must never run within this test's own coroutine")

    mcp_adapter = McpIngressAdapter(
        hook_trigger=_never_drains, maxsize=1,
        # session.emit_audit_event -- the public (event_type, **data) seam,
        # not session._audit_events.emit directly (no private-state reach).
        emit_event=lambda et, **kw: session.emit_audit_event(et, **kw),
    )
    mcp_adapter.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={}))
    mcp_adapter.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={}))
    await mcp_adapter.aclose()
    await settle(session)
    (mcp_call,) = [d for (kind, d) in received if kind == "ingress_bridge_dropped"]

    assert fire_call["source"] != mcp_call["source"], (
        f"_SessionFireBridge and McpIngressAdapter must report distinguishable "
        f"sources on the shared ingress_bridge_dropped kind -- both reported "
        f"{fire_call['source']!r}"
    )
