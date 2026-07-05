"""Tests for #2597 P1 — MCP reconnect resync-read (follow-up to slice ②b, #2607).

②b re-subscribes every tracked URI on a transport-death reconnect, but a
resource that actually changed WHILE the connection was dead never produced a
``resources/updated`` push (the notification never arrived on the dead
transport, and a fresh ``mcp.ClientSession`` has no memory of what it missed).
P1 closes that gap: on a genuine RE-open (not the very first open),
``MCPConnectionService._ensure_open`` emits a SYNTHETIC ``mcp_resource_updated``
(``resync=True``) for each successfully re-subscribed URI, through the exact
same emit_sink + H1 hook-trigger path a real push uses — see
``connection_service.py``'s P1 module-docstring section and
``message_handler.py``'s ``emit_resource_updated``.

Real instances only, per the testing policy: no ``unittest.mock`` /
``MagicMock`` / ``AsyncMock`` / ``patch``. Uses the SAME real low-level MCP
server subprocess ②b's own tests use
(``tests/_support/mcp_subscribable_resources_server.py``) through a REAL
``MCPConnectionService`` + REAL ``EventLog``.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.mcp.client import MCPError
from reyn.mcp.connection_service import MCPConnectionService

_SUPPORT_DIR = Path(__file__).parent / "_support"
_SUBSCRIBABLE_SERVER = _SUPPORT_DIR / "mcp_subscribable_resources_server.py"
_URI = "resource://counter"


def _stdio_cfg(script: Path) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [str(script)]}


async def _wait_for(predicate, *, attempts: int = 100, delay: float = 0.02) -> None:
    """Poll ``predicate()`` until True or give up — mirrors
    test_2597_s2b_resource_subscriptions.py's pattern (async delivery, not
    synchronous with the triggering call)."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


# ── Tier 2: reconnect resync — the core P1 proof ───────────────────────────


@pytest.mark.asyncio
async def test_reconnect_emits_synthetic_resource_updated_for_missed_disconnect_window():
    """Tier 2: THE core P1 proof. Subscribe, kill the subprocess (genuine
    transport death -> F1 heal), then trigger the heal via an IDEMPOTENT read
    (``list_resources`` — NOT ``bump_and_notify``, so the server issues no real
    push at all). The reconnect's re-subscribe loop must still produce an
    ``mcp_resource_updated`` event for the tracked URI — proving a real update
    that could have happened during the disconnect window (and would otherwise
    be silently lost, since the fresh session never received any push for it)
    is surfaced via the synthetic resync signal, not dropped."""
    events = EventLog(subscribers=[])
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)

        # Genuine transport death (mirrors ②b's own reconnect test).
        with pytest.raises(MCPError):
            await client.call_tool("die", {})

        # Trigger the heal via an IDEMPOTENT READ that the server never notifies
        # on — isolates the synthetic resync signal from any real push.
        await client.list_resources()

        await _wait_for(
            lambda: any(e.type == "mcp_resource_updated" for e in events.all())
        )
        matching = [e for e in events.all() if e.type == "mcp_resource_updated"]
        (only_event,) = matching  # exactly one — the synthetic resync, no real push occurred
        assert only_event.data.get("server") == "srv"
        assert only_event.data.get("uri") == _URI
        assert only_event.data.get("resync") is True, (
            "a reconnect-driven re-signal must be distinguishable (resync=True) "
            "from a real server push"
        )
        assert service.subscribed_uris("srv") == [_URI]
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_first_open_emits_no_synthetic_update():
    """Tier 2: the first-open/re-open boundary. The VERY FIRST ``_ensure_open``
    for a server (nothing tracked yet, no prior open) must emit NOTHING — only
    a genuine RE-open after a drop re-signals. Subscribe (which itself opens
    the connection for the first time) and let the connection sit idle: no
    ``mcp_resource_updated`` event of any kind (real or synthetic) may appear,
    since neither a push nor a reconnect happened."""
    events = EventLog(subscribers=[])
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)
        # A second idempotent call against the SAME still-live connection must
        # not re-open (no reconnect happened) and so must not emit anything either.
        await client.list_resources()

        await asyncio.sleep(0.1)  # give any (wrongly) synthetic emit a fair chance
        matching = [e for e in events.all() if e.type == "mcp_resource_updated"]
        assert matching == [], (
            "the first open (and further calls against the still-live "
            "connection) must not emit any mcp_resource_updated — only a "
            "genuine reconnect re-signals"
        )
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_reconnect_resync_also_fires_hook_trigger_identically_to_real_push():
    """Tier 2: the synthetic resync event flows through the SAME H1
    hook-trigger bridge (``enqueue_external_event`` -> drain task -> real async
    ``hook_trigger`` callable) a real push uses — proving a consumer cannot
    tell a resync-driven re-read from a real push except by inspecting
    ``resync`` in the payload. Uses a real recording async callable directly
    wired as ``hook_trigger`` (mirrors test_2608_h1's ``MCPConnectionService(
    hook_trigger=...)`` DI pattern) — confined to connection_service.py, no
    session.py/hooks/ involvement needed to prove this bridge fires."""

    class _RecordingTrigger:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def __call__(self, point: str, template_vars: dict) -> None:
            self.calls.append((point, template_vars))

    trigger = _RecordingTrigger()
    events = EventLog(subscribers=[])
    service = MCPConnectionService(
        emit_sink=lambda et, **d: events.emit(et, **d),
        hook_trigger=trigger,
    )
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)

        with pytest.raises(MCPError):
            await client.call_tool("die", {})
        await client.list_resources()

        await _wait_for(
            lambda: any(e.type == "mcp_resource_updated" for e in events.all())
        )
        await _wait_for(lambda: len(trigger.calls) >= 1)

        (point, template_vars) = trigger.calls[0]
        assert point == "mcp_resource_updated"
        assert template_vars.get("uri") == _URI
        assert template_vars.get("server") == "srv"
        assert template_vars.get("resync") is True
    finally:
        await service.aclose()
