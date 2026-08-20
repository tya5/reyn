"""Tests for #3698 PR-2 — the version-independent subscription port.

Real instances only, per the testing policy: no ``unittest.mock`` /
``MagicMock`` / ``AsyncMock`` / ``patch``. These tests exercise
``reyn.mcp.subscription_port`` both in isolation (Tier 1, the pure
selection predicate) and through a REAL ``MCPConnectionService`` holding a
REAL subprocess connection (Tier 2), against the two test doubles PR-2
itself updated to dual-publish onto the SDK's ``InMemorySubscriptionBus``
(``tests/_support/mcp_fastmcp_echo_server.py``,
``mcp_subscribable_resources_server.py`` — see their own module docstrings
for the live-verified before/after that motivated the dual-publish fix).

Acceptance criteria this file (together with the pre-existing
``test_2597_s2b_mcp_notifications_bridge.py`` / ``test_2597_p1_reconnect_
resync_read.py`` / ``test_2597_s2b_resource_subscriptions.py`` — all three
already exercise the modern era end-to-end, since ``mode="auto"`` is now
the client's own default) are meant to jointly cover:

(a) the three families — tools_list_changed, prompts_list_changed,
    resource_subscriptions — each individually, under a real connection;
(b) an adapter-SELECTION witness, not just "both paths work in isolation";
(c) PR-1's two originally-measured symptoms now demonstrably closed under
    the port (covered by (a) + (b) together: a modern negotiation no
    longer silently drops delivery for either family).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from reyn.core.events.events import EventLog
from reyn.mcp.client import MCPCapabilityError, MCPClient
from reyn.mcp.connection_service import MCPConnectionService
from reyn.mcp.message_handler import ReynMCPMessageHandler
from reyn.mcp.subscription_port import (
    ListenSubscriptionAdapter,
    is_modern_protocol_version,
    select_subscription_adapter,
)
from tests._support.events import collect_events, settle
from tests._support.paths import REPO_ROOT

_SUPPORT_DIR = REPO_ROOT / "tests" / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"
_SUBSCRIBABLE_SERVER = _SUPPORT_DIR / "mcp_subscribable_resources_server.py"
_NO_SUBSCRIBE_SERVER = _SUPPORT_DIR / "mcp_resources_no_subscribe_server.py"
_URI = "resource://counter"


def _stdio_cfg(script: Path) -> dict:
    # #3698 review ruling (#4559): "legacy" is now MCPClient's own default
    # (see client.py's module docstring) — every test in THIS file is
    # specifically about the modern-era subscription port, so each
    # connection here opts in explicitly via protocol_mode="auto" rather
    # than relying on a default this file never actually wants.
    return {
        "type": "stdio", "command": sys.executable, "args": [str(script)],
        "protocol_mode": "auto",
    }


# ── Tier 1: is_modern_protocol_version — reyn's own predicate ──────────────


def test_is_modern_protocol_version_true_for_every_modern_version():
    """Tier 1: every entry in the SDK's own MODERN_PROTOCOL_VERSIONS reads as
    modern — the predicate is duck-typed against that tuple, not a hardcoded
    string, so this is reyn's own logic, not a third-party promise."""
    assert MODERN_PROTOCOL_VERSIONS, "the SDK's own tuple must be non-empty for this test to mean anything"
    for version in MODERN_PROTOCOL_VERSIONS:
        assert is_modern_protocol_version(version) is True


def test_is_modern_protocol_version_false_for_legacy_and_none():
    """Tier 1: a pre-2026-07-28 version string and ``None`` (no connection
    negotiated yet) both read as not-modern."""
    assert is_modern_protocol_version("2025-11-25") is False
    assert is_modern_protocol_version(None) is False


# ── Tier 1: select_subscription_adapter — selection is version-only ────────


@pytest.mark.asyncio
async def test_select_subscription_adapter_is_version_only_handler_never_gates_selection():
    """Tier 1: #3698 review ruling — selection is PURELY version-based;
    ``handler`` never gates WHICH adapter is picked (only what the picked
    adapter does with delivered events). An earlier version of this
    function special-cased ``handler is None`` to always return legacy —
    live-verified FALSE (#3698 review): the legacy RPC does not exist on
    the wire once negotiation is modern, regardless of handler presence,
    so a handler-less caller on a modern-negotiated connection still needs
    the listen adapter (it just gets no local dispatch — see
    ListenSubscriptionAdapter's own docstring). Driven against a real live
    connection (this file's own protocol_mode="auto" opt-in — see
    _stdio_cfg — since "legacy" is the client's own default post-#4559
    ruling), not a faked client."""
    async with MCPClient(_stdio_cfg(_ECHO_SERVER)) as client:
        adapter = select_subscription_adapter(client, None)
        assert isinstance(adapter, ListenSubscriptionAdapter), (
            "a modern-negotiated connection must get the listen adapter "
            "even with no handler installed — the legacy RPC is not a "
            "valid fallback under modern negotiation"
        )
        await adapter.close()


# ── Tier 1: a genuinely listen-incapable double — #4559's "column B" ───────
#    (reyn's own test infrastructure that structurally can't express modern
#    behavior) witness, requested explicitly by the #3698 review ruling.


@pytest.mark.asyncio
async def test_no_subscribe_double_reads_subscribe_false_even_under_modern_negotiation():
    """Tier 1: unlike EVERY ``MCPServer``-built double in this directory
    (which registers ``subscriptions/listen`` unconditionally — see
    ``mcp_resources_no_subscribe_server.py``'s own module docstring for the
    live-verified finding this documents), a low-level ``Server`` built
    WITHOUT ``on_subscriptions_listen`` correctly reads
    ``resources.subscribe=False`` even when the CONNECTION itself
    negotiates modern (this file's protocol_mode="auto" opt-in) — proving
    the gap is in ``MCPServer``'s own unconditional wiring, not in modern
    negotiation itself, and giving #4559's "column B" enumeration one
    concrete, tested example rather than only a claim."""
    async with MCPClient(_stdio_cfg(_NO_SUBSCRIBE_SERVER), server_name="no-sub-srv") as client:
        assert client.negotiated_version in MODERN_PROTOCOL_VERSIONS
        assert client.supports("resources") is True
        with pytest.raises(MCPCapabilityError) as exc_info:
            await client.subscribe_resource("resource://pid")
        assert "subscribe" in str(exc_info.value)


# ── Tier 2: adapter-SELECTION witness — the acceptance criterion itself ────


@pytest.mark.asyncio
async def test_mcp_initialized_names_the_selected_adapter_class():
    """Tier 2: THE adapter-selection witness lead-coder named as an
    acceptance criterion — not "both paths work in isolation" but that
    WHICH one a given connection picked is independently observable. A real
    connection against a modern-supporting double negotiates modern (the
    client's own "auto" default) and the ``mcp_initialized`` audit event
    names ``ListenSubscriptionAdapter`` as the selected class."""
    events = EventLog(subscribers=[])
    collected = collect_events(events)
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        await service.get("srv", _stdio_cfg(_ECHO_SERVER))

        await settle(events)
        matching = [e for e in collected if e.type == "mcp_initialized"]
        (only_event,) = matching  # exactly one — the single (re)connect this test drove
        assert only_event.data.get("server") == "srv"
        assert only_event.data.get("subscription_adapter") == "ListenSubscriptionAdapter", (
            "a modern-supporting double must select the listen adapter — the "
            "selection itself, not just delivery, is what this event witnesses"
        )
    finally:
        await service.aclose()


# ── LegacySubscriptionAdapter — KNOWN COVERAGE GAP, not silently omitted ───
#
# There is currently no LIVE test of LegacySubscriptionAdapter.open() against
# a real pre-2026-07-28-negotiated connection. An earlier version of this
# file had a test that hand-constructed LegacySubscriptionAdapter directly
# against a modern-negotiated MCPClient (both reyn-owned test doubles now
# advertise `subscriptions/listen`, so every live connection in this repo
# negotiates modern) — deleted per the testing policy's six questions,
# question 3: that is a configuration only the test itself built; production
# code (select_subscription_adapter) never constructs LegacySubscriptionAdapter
# for a modern-negotiated client, so the test was pinning a shape nothing
# real produces, and its "proof" (the legacy RPC succeeds) was actually a
# 404 once the double gained listen support — the exact bug this PR fixes
# elsewhere. Closing this gap for real needs a genuinely legacy-only (pre-
# 2026-07-28) test double, which does not exist in this repo today — left
# for whoever adds one, not invented here as a second implementation of a
# fake collaborator (the testing policy's Mock-vs-Fake ban).


# ── Tier 2: prompts_list_changed — parity with the existing tools_list_changed
#    coverage in test_2597_s2b_mcp_notifications_bridge.py; that file never
#    added a prompts sibling even though the SDK/on_prompt_list_changed path
#    has existed since before PR-2 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_list_changed_notification_emits_event():
    """Tier 2: a REAL server-pushed prompts_list_changed — via the modern
    ``Client.listen()`` stream (the double's own dual-publish, see its
    module docstring) since this connection negotiates modern by default —
    lands as an ``mcp_prompt_list_changed`` event on the EventLog, mirroring
    the existing tools_list_changed coverage for the sibling family."""
    events = EventLog(subscribers=[])
    collected = collect_events(events)
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_ECHO_SERVER))
        result = await client.call_tool("notify_prompt_list_changed", {})
        assert result["isError"] is False

        import asyncio

        # #3748-style: unbounded per the owner's testing policy — the loop
        # condition IS the terminating check.
        while not any(e.type == "mcp_prompt_list_changed" for e in collected):
            await asyncio.sleep(0.02)

        matching = [e for e in collected if e.type == "mcp_prompt_list_changed"]
        (only_event,) = matching  # exactly one — the single real notification sent
        assert only_event.data.get("server") == "srv"
    finally:
        await service.aclose()


# ── Tier 2: dual-publish, single-emit — the #3698 review's own acceptance
#    criterion (lead-coder: "turn what you stumbled onto into an acceptance
#    criterion"). Both test doubles fire BOTH the legacy notification AND
#    the modern listen() event for every notify_*/bump_and_notify call (see
#    their own module docstrings) — a real, plausible migration-period
#    server shape, not just a testing artifact (#3698 review ruling). reyn
#    must emit exactly ONE event per real change regardless — see
#    ReynMCPMessageHandler.set_listen_honored's docstring for the mechanism
#    (per-family legacy-dispatch suppression while listen honors that
#    family) this proves. ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dual_firing_server_still_emits_tool_list_changed_exactly_once():
    """Tier 2: THE acceptance witness — a server that fires BOTH channels
    for the SAME logical change must still produce exactly ONE
    ``mcp_tool_list_changed`` on reyn's EventLog, not two. Named explicitly
    (distinct from test_2597_s2b_mcp_notifications_bridge.py's own
    single-count assertion, which predates this ruling and would have
    silently regressed to double-counting without the suppression
    mechanism this test names directly)."""
    events = EventLog(subscribers=[])
    collected = collect_events(events)
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_ECHO_SERVER))
        result = await client.call_tool("notify_tool_list_changed", {})
        assert result["isError"] is False

        import asyncio

        while not any(e.type == "mcp_tool_list_changed" for e in collected):
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)  # give a WOULD-BE second (legacy) emit a fair chance to land

        matching = [e for e in collected if e.type == "mcp_tool_list_changed"]
        (only_event,) = matching  # exactly one, despite the double firing server-side
        assert only_event.data.get("server") == "srv"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_dual_firing_server_still_emits_resource_updated_exactly_once():
    """Tier 2: same witness, for the resource_subscriptions family —
    ``mcp_subscribable_resources_server.py``'s ``bump_and_notify`` ALSO
    dual-fires (``send_resource_updated`` AND ``bus.publish``)."""
    events = EventLog(subscribers=[])
    collected = collect_events(events)
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)
        result = await client.call_tool("bump_and_notify", {})
        assert result["isError"] is False

        import asyncio

        while not any(e.type == "mcp_resource_updated" for e in collected):
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)

        matching = [e for e in collected if e.type == "mcp_resource_updated"]
        (only_event,) = matching  # exactly one, despite the double firing server-side
        assert only_event.data.get("uri") == _URI
    finally:
        await service.aclose()


# ── Tier 1: ListenSubscriptionAdapter re-dispatches to the handler's own
#    methods, not a re-derived shape (drives the REAL __call__/dispatch path
#    against a real handler instance, no live server needed for this one) ──


@pytest.mark.asyncio
async def test_listen_adapter_dispatch_routes_each_event_kind_to_the_matching_handler_method():
    """Tier 1: ``_dispatch``'s per-event-kind routing is reyn's OWN
    control flow (which handler method a given SDK event kind reaches) —
    drives it directly against a real ``ReynMCPMessageHandler`` sink, no
    live connection needed since this is testing the routing table, not
    delivery over the wire (that's the Tier 2 tests above)."""
    from mcp.shared.subscriptions import (
        PromptsListChanged,
        ResourcesListChanged,
        ResourceUpdated,
        ToolsListChanged,
    )

    events = EventLog(subscribers=[])
    collected = collect_events(events)
    handler = ReynMCPMessageHandler(lambda et, **d: events.emit(et, **d), "srv")

    # No live client needed for _dispatch itself — construct the adapter
    # with client=None (never read by _dispatch) and drive the method
    # directly, mirroring the notifications-bridge file's own direct-
    # __call__ pattern for the same reason (isolating routing from wire I/O).
    adapter = ListenSubscriptionAdapter(client=None, handler=handler)

    await adapter._dispatch(ToolsListChanged())
    await adapter._dispatch(PromptsListChanged())
    await adapter._dispatch(ResourceUpdated(uri=_URI))

    await settle(events)
    types_seen = [e.type for e in collected]
    assert types_seen.count("mcp_tool_list_changed") == 1
    assert types_seen.count("mcp_prompt_list_changed") == 1
    matching_updated = [e for e in collected if e.type == "mcp_resource_updated"]
    (only_update,) = matching_updated
    assert only_update.data.get("uri") == _URI
    assert only_update.data.get("resync") is False, (
        "a real modern-era push is not a reconnect resync — resync=False "
        "distinguishes it from #2597 P1's synthetic re-signal"
    )

    # ResourcesListChanged is out of scope (message_handler.py's own
    # docstring: "no reyn caller subscribes to the resource LIST changing")
    # — dispatching it must not add any NEW event beyond the 3 already
    # asserted above (compares the collected list's own state before/after,
    # not a fixed count).
    before = list(collected)
    await adapter._dispatch(ResourcesListChanged())
    await settle(events)
    assert collected == before, (
        f"ResourcesListChanged must produce no event — collected grew from "
        f"{[e.type for e in before]} to {[e.type for e in collected]}"
    )
