"""#3698's own closing witness: a real reyn client, connected to a real
server, actually settling negotiation on protocol version 2026-07-28 — and
resource-subscription delivery surviving under that version's revised
(stateless-capable, `subscriptions/listen`-based) lifecycle.

## Why this file exists separately from the pre-existing PR-2 coverage

``test_3698_pr2_subscription_port.py`` already exercises real modern-era
connections (``protocol_mode="auto"``) and already has a full subscribe ->
trigger -> observe round trip
(``test_dual_firing_server_still_emits_resource_updated_exactly_once``).
What none of that coverage does is assert the LITERAL negotiated-version
string on the ``mcp_initialized`` audit event — every existing assertion
checks either ``isinstance(str) and truthy`` or membership in
``MODERN_PROTOCOL_VERSIONS`` (a tuple that happens to have exactly one
entry today, so passing that check IS already equivalent to "settled on
2026-07-28", just never stated as literally as this issue's own acceptance
criteria ask for). This file states it literally, in one place, tied to
the SAME connection that also proves subscription delivery — so the two
halves of #3698's own close condition are witnessed together, not spread
across two tests neither of which claims to be the closing evidence.

## What this file does NOT (and structurally cannot) do

A live comparison against a LEGACY (pre-2026-07-28) real connection. Every
test-double MCP server in this repo already advertises
``subscriptions/listen`` (see ``test_3698_pr2_subscription_port.py``'s own
"LegacySubscriptionAdapter — KNOWN COVERAGE GAP" note), so there is no
legacy-negotiating real server left in-repo to connect to without building
a second fake collaborator — banned by the testing policy's Mock-vs-Fake
rule, and explicitly declined by that file's own review for the same
reason. The legacy side of #3698's comparison is the real, already-measured
evidence in the issue body itself: reyn's live connections to the broker
MCP server (7 real sessions, ``mcp_initialized`` -> ``negotiated_version:
"2025-11-25"``, ``mcp_resource_subscribed`` -> a real broker resource) — a
production fact this test does not and cannot reproduce, only cite. Put
side by side: the SAME reyn client, the SAME subscribe/trigger/observe
shape, succeeds against a real 2025-11-25 server (broker, in production)
AND a real 2026-07-28 server (this file, live) — the stateless-lifecycle
revision does not break resource-subscription delivery.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.mcp.connection_service import MCPConnectionService
from tests._support.events import collect_events
from tests._support.paths import REPO_ROOT

_SUPPORT_DIR = REPO_ROOT / "tests" / "_support"
_SUBSCRIBABLE_SERVER = _SUPPORT_DIR / "mcp_subscribable_resources_server.py"
_URI = "resource://counter"


def _stdio_cfg(script: Path) -> dict:
    return {
        "type": "stdio", "command": sys.executable, "args": [str(script)],
        "protocol_mode": "auto",
    }


@pytest.mark.asyncio
async def test_live_connection_negotiates_exactly_2026_07_28_and_delivers_a_real_subscription_update():
    """Tier 2: #3698's closing witness. A REAL subprocess MCP server (no
    mocks), a REAL reyn MCPConnectionService connection, ``protocol_mode=
    "auto"`` (the SDK's own negotiate-up behavior, opted in explicitly —
    see this file's ``_stdio_cfg``). Asserts, on the SAME connection:

    (3) the ``mcp_initialized`` audit event's ``negotiated_version`` is the
        LITERAL string ``"2026-07-28"`` — not "a string", not "in the
        modern tuple" (both already true elsewhere in this repo's suite),
        the exact value #3698's acceptance criteria name.
    (4) resource-subscription still delivers a real
        ``notifications/resources/updated``-derived ``mcp_resource_updated``
        audit event end-to-end under this version's revised
        (``subscriptions/listen``-based) lifecycle — the concern #3698's
        own body raises about the stateless revision.
    """
    events = EventLog(subscribers=[])
    collected = collect_events(events)
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))

        init_events = [e for e in collected if e.type == "mcp_initialized"]
        (init_event,) = init_events  # exactly one — the single connect this test drove
        assert init_event.data.get("negotiated_version") == "2026-07-28", (
            f"expected a live negotiation to settle on the literal string "
            f"'2026-07-28' — got {init_event.data.get('negotiated_version')!r}. "
            f"This IS the #3698 witness: if this fails, the SDK/server pair "
            f"did not actually negotiate modern, regardless of what any "
            f"other test in this repo asserts about 'in MODERN_PROTOCOL_VERSIONS'."
        )
        assert init_event.data.get("subscription_adapter") == "ListenSubscriptionAdapter"

        await client.subscribe_resource(_URI)
        result = await client.call_tool("bump_and_notify", {})
        assert result["isError"] is False

        while not any(e.type == "mcp_resource_updated" for e in collected):
            await asyncio.sleep(0.02)

        updated_events = [e for e in collected if e.type == "mcp_resource_updated"]
        assert any(e.data.get("uri") == _URI for e in updated_events), (
            "resource-subscription delivery did not survive under the "
            "2026-07-28 stateless-capable (subscriptions/listen) lifecycle "
            "— this is the exact regression #3698's body worries about."
        )
    finally:
        await service.aclose()
