"""Tier 1: #5515 — ``_BoundedEventBridge.deliver``'s own queue-full drop is
now fail-visible, mirroring ``HookBus._audit_drop``'s already-landed #2886
discipline (``tests/hooks/test_hook_event_bus_0059_phase4a.py``) one layer
over: the SAME first-drop/every-Nth cadence, a metadata-only
``ingress_bridge_dropped`` P6 audit-event via the injected ``emit_event``
sink, never the dropped event's own payload.

Before this fix the drop was ``logger.warning``-only — invisible to
``reyn events``/``session.subscribe_audit_events`` (#2886 closed the
identical gap for ``HookBus``'s own subscriber-queue overflow; this bridge
was the sibling the module docstrings already describe as sharing that
overflow shape, and it never got the audit half).

This file drives ``_BoundedEventBridge`` directly (a module-private class,
but its OWN public constructor/``deliver``/``dropped_count`` surface — not
another object's private state) with a real, tiny-``maxsize`` queue and a
``hook_trigger`` that never drains, so overflow is forced deterministically
within one coroutine (no ``await`` between ``deliver()`` calls, so the
lazily-started drain task never gets a chance to run and empty the queue —
same non-interleaving argument the existing bus.py suite relies on).

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import pytest

from reyn.hooks.event import HookEvent
from reyn.hooks.ingress import _AUDIT_EVERY_N_DROPS, _BoundedEventBridge


def _recorder():
    """A real recording callable (no MagicMock/patch) — same shape
    test_hook_event_bus_0059_phase4a.py's own ``_recorder`` uses."""
    calls: "list[tuple]" = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))

    return record, calls


async def _never_drains(*_args, **_kwargs) -> None:
    """A ``hook_trigger`` that is never actually awaited by this test (the
    queue overflows before the lazily-started drain task gets a chance to
    run — see this module's own docstring) but must still be a real async
    callable, matching ``HookTrigger``'s own signature."""
    raise AssertionError("drain must never run within this test's own coroutine")


@pytest.mark.asyncio
async def test_queue_full_drop_is_fail_visible():
    """Tier 1: overflowing the bridge's bounded queue increments
    ``dropped_count()`` and fires a metadata-only ``ingress_bridge_dropped``
    P6 audit-event on the FIRST drop — ``source``/``point``/``drop_count``
    only, never the dropped event's own kind/payload.

    Strip-falsify: remove the ``self._audit_drop(event)`` call in
    ``_BoundedEventBridge.deliver`` (or the ``self._drop_count += 1`` line)
    and this test goes RED — no ``ingress_bridge_dropped`` call / 0 calls
    recorded (performed during review: reverting the call site makes
    ``dropped_calls`` empty and the ``(only,)`` unpack below raises
    ``ValueError``, not silently pass over an empty collection)."""
    emit_event, calls = _recorder()
    bridge = _BoundedEventBridge(
        hook_trigger=_never_drains, maxsize=1, adapter_name="McpIngressAdapter",
        emit_event=emit_event,
    )

    bridge.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={"n": 0}))
    # Queue now holds 1 (its maxsize) undrained event; the NEXT deliver
    # overflows it (drop-newest — the new event itself is dropped).
    bridge.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={"n": 1}))

    assert bridge.dropped_count() == 1

    dropped_calls = [c for c in calls if c[0] and c[0][0] == "ingress_bridge_dropped"]
    (only,) = dropped_calls  # exactly one audit-event fired — unpack-must-flip
    (_args, kwargs) = only
    assert kwargs["source"] == "McpIngressAdapter"
    assert kwargs["point"] == "mcp_resource_updated"
    assert kwargs["drop_count"] == 1
    # never the dropped event's content
    assert "kind" not in kwargs and "payload" not in kwargs

    await bridge.aclose()


@pytest.mark.asyncio
async def test_sustained_overflow_audits_first_then_every_nth_not_every_drop():
    """Tier 1: under sustained overflow the audit-event fires on the first
    drop and then only every Nth drop — not once per drop (``deliver`` is a
    sync/never-raises hot path; auditing every drop would flood the audit
    log under a burst faster than hooks can be dispatched). ``dropped_count()``
    still counts every single drop regardless of audit cadence."""
    emit_event, calls = _recorder()
    bridge = _BoundedEventBridge(
        hook_trigger=_never_drains, maxsize=1, adapter_name="FsIngressAdapter",
        emit_event=emit_event,
    )

    total_delivers = _AUDIT_EVERY_N_DROPS + 2  # 1 fills the queue, the rest all drop
    for i in range(total_delivers):
        bridge.deliver(HookEvent(kind="builtin:external:file_changed", payload={"n": i}))

    expected_drops = total_delivers - 1
    assert bridge.dropped_count() == expected_drops

    dropped_calls = [c for c in calls if c[0] and c[0][0] == "ingress_bridge_dropped"]
    # first drop (drop_count == 1) + the Nth drop (drop_count == _AUDIT_EVERY_N_DROPS) —
    # exactly two audit-events fired, unpack-must-flip if the cadence regresses.
    (first_call, nth_call) = dropped_calls
    assert first_call[1]["drop_count"] == 1
    assert nth_call[1]["drop_count"] == _AUDIT_EVERY_N_DROPS

    await bridge.aclose()


@pytest.mark.asyncio
async def test_source_distinguishes_which_adapters_bridge_dropped():
    """Tier 1: lead-coder review requirement (#5515) — since
    ``ingress_bridge_dropped`` is a SHARED kind (deliberately not split
    per-adapter, matching ``composer_dropped``'s own shared-kind precedent),
    the ``source`` field alone must let a reader tell which bridge dropped.
    Both real adapter names (``McpIngressAdapter``/``FsIngressAdapter`` —
    the literal strings ``ingress.py``'s two adapter classes pass as their
    own ``adapter_name``) are proven NOT to collide here, independent of
    ``test_queue_full_drop_is_fail_visible``'s own single-bridge check
    above."""
    mcp_emit, mcp_calls = _recorder()
    fs_emit, fs_calls = _recorder()
    mcp_bridge = _BoundedEventBridge(
        hook_trigger=_never_drains, maxsize=1, adapter_name="McpIngressAdapter",
        emit_event=mcp_emit,
    )
    fs_bridge = _BoundedEventBridge(
        hook_trigger=_never_drains, maxsize=1, adapter_name="FsIngressAdapter",
        emit_event=fs_emit,
    )

    for bridge in (mcp_bridge, fs_bridge):
        bridge.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={}))
        bridge.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={}))

    (mcp_call,) = [c for c in mcp_calls if c[0] and c[0][0] == "ingress_bridge_dropped"]
    (fs_call,) = [c for c in fs_calls if c[0] and c[0][0] == "ingress_bridge_dropped"]
    assert mcp_call[1]["source"] == "McpIngressAdapter"
    assert fs_call[1]["source"] == "FsIngressAdapter"
    assert mcp_call[1]["source"] != fs_call[1]["source"]

    await mcp_bridge.aclose()
    await fs_bridge.aclose()


@pytest.mark.asyncio
async def test_no_emit_event_sink_is_still_silent_but_never_raises():
    """Tier 1: ``emit_event=None`` (a session/test double with no audit sink
    wired — the pre-#5515 default) must still be a byte-identical no-op for
    the audit half: ``deliver`` never raises, and ``dropped_count()`` still
    counts the drop even though nothing was there to receive an
    audit-event."""
    bridge = _BoundedEventBridge(
        hook_trigger=_never_drains, maxsize=1, adapter_name="McpIngressAdapter",
    )

    bridge.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={"n": 0}))
    bridge.deliver(HookEvent(kind="builtin:external:mcp_resource_updated", payload={"n": 1}))  # must not raise

    assert bridge.dropped_count() == 1

    await bridge.aclose()
