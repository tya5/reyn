"""Tests for #2608 H1 — the external-event->hooks TRIGGER mechanism.

H1 adds the FIRST external-event hook-point, ``mcp_resource_updated``: a REAL
MCP ``resources/updated`` push (from a subscribed resource) fires a
user-configured hook via a bounded sync->async bridge from
``ReynMCPMessageHandler`` (the MCP receive-loop task) into
``HookDispatcher.dispatch`` (the session's event loop).

Real instances only, per the testing policy: no ``unittest.mock`` /
``MagicMock`` / ``AsyncMock`` / ``patch``. The end-to-end proof spawns the SAME
real low-level MCP server subprocess #2597 slice ②b's own tests use
(``tests/_support/mcp_subscribable_resources_server.py``) through a REAL
``Session`` (so the per-session ``HookDispatcher`` wiring — the deferred
``hook_trigger`` closure over ``self._hook_dispatcher.dispatch`` in
``session.py`` — is exercised, not bypassed), with a REAL ``hooks_config``
loaded by the production ``load_hooks`` seam and a REAL ``template_push``
action landing in the session's own (public) inbox.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.mcp.connection_service import MCPConnectionService
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session
from tests._support.events import collect_events
from tests._support.paths import REPO_ROOT

_SUPPORT_DIR = REPO_ROOT / "tests" / "_support"
_SUBSCRIBABLE_SERVER = _SUPPORT_DIR / "mcp_subscribable_resources_server.py"
_URI = "resource://counter"


def _stdio_cfg(script: Path) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [str(script)]}


async def _wait_for(predicate, *, delay: float = 0.02) -> None:
    """The push notification arrives asynchronously on the MCP receive-loop task,
    not synchronously with the triggering call. Unbounded per the owner's
    testing policy (docs/deep-dives/contributing/testing.md, ## Time): no test
    carries a time budget, marker or in-body -- a slower environment only makes
    this slower, never fail it; CI's --timeout=120 is the blast-radius
    kill-switch, not a contract.
    """
    while not predicate():
        await asyncio.sleep(delay)


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return make_session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


# ---------------------------------------------------------------------------
# Tier 1: schema — the new hook-point is registered and config-loadable
# ---------------------------------------------------------------------------


def test_mcp_resource_updated_is_an_allowed_hook_point():
    """Tier 1: ``mcp_resource_updated`` is registered in ALLOWED_HOOK_POINTS
    alongside the 6 lifecycle points — the schema-level gate a hooks.yaml
    entry with ``on: mcp_resource_updated`` must pass."""
    from reyn.hooks.schema import ALLOWED_HOOK_POINTS

    assert "mcp_resource_updated" in ALLOWED_HOOK_POINTS


def test_mcp_resource_updated_hook_loads_via_production_loader():
    """Tier 1: a ``hooks:`` config entry with ``on: mcp_resource_updated`` and a
    ``template_push`` action parses through the REAL ``load_hooks`` seam (the
    same one Session uses) into a HookRegistry that serves it back for that
    point — no HookConfigError, matcher stays unset (reserved, not H1 scope)."""
    from reyn.hooks.loader import load_hooks

    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "resource {{ uri }} updated"},
        },
    ]
    registry = load_hooks(raw)
    (hook,) = registry.hooks_for("mcp_resource_updated")  # exactly one registered for this point
    assert hook.matcher is None  # reserved/uninterpreted for H1


# ---------------------------------------------------------------------------
# Tier 2: real Session + real subprocess MCP server — end-to-end trigger proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_mcp_push_fires_configured_hook_into_session_inbox(tmp_path):
    """Tier 2: THE core H1 proof. A REAL Session with a ``mcp_resource_updated``
    ``template_push`` hook configured, subscribed to a REAL server's resource
    through the session's OWN (per-session) ``MCPConnectionService`` — a real
    ``notifications/resources/updated`` push from that server both (a) emits the
    existing ``mcp_resource_updated`` EventLog event (②b, unchanged) AND (b) now
    ALSO fires the configured hook, landing the templated push in the session's
    public inbox. Proves: subscribe -> server push -> bounded sync->async bridge
    -> this session's OWN HookDispatcher -> template_push -> inbox."""
    hooks_config = [
        {
            "on": "mcp_resource_updated",
            "template_push": {
                "message": "[{{ server }}] {{ uri }} updated for {{ agent_name }}",
                "wake": True,
            },
        },
    ]
    session = _make_session(tmp_path, hooks_config=hooks_config)
    collected = collect_events(session._audit_events)
    try:
        client = await session._mcp_connection_service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)

        result = await client.call_tool("bump_and_notify", {})
        assert result["isError"] is False

        # The EventLog side (②b, unchanged) still fires — confirms the receive-loop
        # actually processed the push before we assert on the NEW hook side.
        await _wait_for(
            lambda: any(e.type == "mcp_resource_updated" for e in collected)
        )

        # #2608 H1: the hook side — the templated push landed in the (public) inbox.
        await _wait_for(lambda: not session.inbox.empty())
        kind, payload = session.inbox.get_nowait()
        assert kind == "hook"
        assert payload["wake"] is True
        assert payload["text"] == f"[srv] {_URI} updated for test-agent"
        assert payload["name"] == "mcp_resource_updated"  # no ``name:`` set -> defaults to the point
    finally:
        await session._mcp_connection_service.aclose()


@pytest.mark.asyncio
async def test_no_configured_hook_leaves_hook_side_a_pure_noop(tmp_path):
    """Tier 2: empty-hook-registry equivalence. Same real subscribe+push, but with
    NO ``mcp_resource_updated`` hook configured — the EventLog side still fires
    (②b unchanged), but the hook side is a pure no-op: the inbox stays empty. This
    is the byte-identical-to-today behavior the H1 design requires when no such
    hook is configured for the session."""
    session = _make_session(tmp_path, hooks_config=None)
    collected = collect_events(session._audit_events)
    try:
        client = await session._mcp_connection_service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)

        result = await client.call_tool("bump_and_notify", {})
        assert result["isError"] is False

        await _wait_for(
            lambda: any(e.type == "mcp_resource_updated" for e in collected)
        )
        # Give the (no-op) hook dispatch a fair chance to have run before asserting
        # the negative — the drain task still runs, HookDispatcher.dispatch() is
        # just a no-op over an empty hooks_for("mcp_resource_updated") list.
        await asyncio.sleep(0.1)
        assert session.inbox.empty()
    finally:
        await session._mcp_connection_service.aclose()


# ---------------------------------------------------------------------------
# Tier 2: #2875 F1 — MCP production path actually reaches McpIngressAdapter.to_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_mcp_push_routes_through_mcp_ingress_adapter_to_event(tmp_path, monkeypatch):
    """Tier 2: #2875 F1 — the production MCP ingress path
    (``ReynMCPMessageHandler.emit_resource_updated`` ->
    ``MCPConnectionService._mcp_to_hook_event``) actually reaches
    ``reyn.hooks.ingress.McpIngressAdapter.to_event``. Phase 2 (#2872) added
    ``to_event``, but the MCP call site kept building the payload inline via
    ``build_hook_payload`` directly (bypassing the adapter), so ``to_event`` was
    production-dead for MCP — the §6 Ingress-Adapter unify was 3/4 wired, not
    4/4. Record-then-delegate around the REAL ``McpIngressAdapter.to_event``
    (the same idiom ``test_hook_event_schema_registry_sync_0059.py`` uses for
    ``HookDispatcher.dispatch`` — every side effect still runs for real, only
    the observation is added) and drive the SAME real subprocess push as the
    core H1 proof above.

    Strip-falsify: if the rewire were undone (``message_handler.py`` reverted
    to building the payload itself, bypassing ``to_event``), ``calls`` stays
    empty, ``_wait_for`` times out, and the ``(call,) = calls`` unpack below
    raises — RED."""
    from reyn.hooks.ingress import McpIngressAdapter

    calls: list[tuple[str | None, str, bool]] = []
    original = McpIngressAdapter.to_event

    def _recording_to_event(self, uri, *, server, agent_name, resync):
        calls.append((uri, server, resync))
        return original(self, uri, server=server, agent_name=agent_name, resync=resync)

    monkeypatch.setattr(McpIngressAdapter, "to_event", _recording_to_event)

    hooks_config = [
        {"on": "mcp_resource_updated", "template_push": {"message": "{{ uri }}"}},
    ]
    session = _make_session(tmp_path, hooks_config=hooks_config)
    try:
        client = await session._mcp_connection_service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)

        result = await client.call_tool("bump_and_notify", {})
        assert result["isError"] is False

        await _wait_for(lambda: bool(calls))
        (call,) = calls  # exactly one push -> exactly one to_event call
        assert call == (_URI, "srv", False)
    finally:
        await session._mcp_connection_service.aclose()


# ---------------------------------------------------------------------------
# Tier 2: bounded sync->async bridge — overflow drops, never blocks
# ---------------------------------------------------------------------------


class _BlockingTrigger:
    """A real recording async callable that blocks on an ``asyncio.Event`` — used
    to hold the drain task's first dispatch open long enough to observe the
    bounded queue's overflow-drop behavior deterministically (not a mock: a plain
    async function object, exactly the ``hook_trigger`` DI shape).

    #5516: batch-shaped — ``calls`` records ``(point, payloads,
    skipped_session_wide)`` per BATCH, not per event (folding means a
    burst that fits entirely in the queue before the drain task gets its
    first scheduling chance becomes ONE call carrying all of it)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list, int]] = []
        self.gate = asyncio.Event()

    async def __call__(
        self, point: str, payloads: list, skipped_session_wide: int = 0,
    ) -> None:
        self.calls.append((point, payloads, skipped_session_wide))
        await self.gate.wait()


@pytest.mark.asyncio
async def test_bounded_queue_drops_overflow_without_blocking_or_raising():
    """Tier 2: F7-1's load-bearing safety property. A burst of
    ``enqueue_external_event`` calls (all synchronous, no ``await`` between them —
    so the drain task never gets a scheduling chance mid-burst) beyond the bounded
    queue's capacity must never raise or block the caller; the excess is DROPPED
    (never queued unboundedly).

    #5516: because the ENTIRE burst lands in the queue before the drain
    task ever runs (no await interleaves), the fold mechanism (#5516,
    ``reyn.hooks.fold.drain_folded``) picks up the first item, reads
    ``qsize()`` (== every remaining queued item, since nothing else can
    have arrived yet), and folds ALL of them into ONE ``hook_trigger``
    call — never one call per event. Exactly the overflow drop (never
    fewer items delivered, never more) is what this test now asserts via
    the folded batch's own length, not via a call count."""
    from reyn.mcp.connection_service import _HOOK_EVENT_QUEUE_MAXSIZE

    trigger = _BlockingTrigger()
    service = MCPConnectionService(hook_trigger=trigger)
    try:
        burst = _HOOK_EVENT_QUEUE_MAXSIZE + 20
        for i in range(burst):
            # Must never raise / never block, regardless of queue fullness.
            service.enqueue_external_event("mcp_resource_updated", {"i": i})

        # Let the drain task start; its one dispatch blocks on the gate.
        await asyncio.sleep(0.05)
        (first_call,) = trigger.calls  # exactly one BATCH call so far (blocked on the gate)
        point, payloads, skipped = first_call
        assert point == "mcp_resource_updated"
        assert len(payloads) == _HOOK_EVENT_QUEUE_MAXSIZE, (
            "the bounded queue must drop exactly the overflow — the folded batch "
            "must carry everything that fit (never fewer, a stall) and never more "
            f"(unbounded growth) -- got {len(payloads)} payloads"
        )
        trigger.gate.set()  # release — nothing further is queued (no second batch expected)
        await asyncio.sleep(0.05)

        # Exactly ONE batch call — tuple-unpacking raises its own clear
        # error if the whole burst did NOT fold into a single call, without
        # pinning a literal count via len(...).
        (only_call,) = trigger.calls
        assert only_call is first_call, (
            "the whole burst landed before the drain task's first scheduling "
            "chance, so it must fold into exactly ONE batch call, not a second"
        )
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_no_hook_trigger_wired_enqueue_is_a_pure_noop():
    """Tier 2: ``hook_trigger=None`` (the ephemeral MCPClientPool path, or any
    session that never wires one) — ``enqueue_external_event`` never raises and
    ``aclose`` afterward never raises either (nothing was ever created to cancel).
    Public-surface smoke: byte-identical to a pre-H1 ``MCPConnectionService``
    that never had this method called on it at all."""
    service = MCPConnectionService()  # hook_trigger defaults to None
    service.enqueue_external_event("mcp_resource_updated", {"server": "srv"})  # must not raise
    await service.aclose()  # must not raise
