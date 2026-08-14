"""Tier 2: #4686 — MCP subscription visibility, the connection_service half.

The issue's owner-approved design (issue #4686, architect + lead-coder
thread) settled on a specific 3-state contract for "is this URI actually
live": ``unhonored_uris(server)`` returns ``None`` when honored-ness can't
be determined at all for that server (a Legacy connection — the pre-
2026-07-28 ``resources/subscribe`` protocol has no per-URI ack), or the
subset of the REQUESTED set the server did NOT confirm (a Listen
connection, which CAN report it). ``subscribed_uris`` stays the requested
set either way — the row population is never honored-only, so a URI the
server declined must not silently vanish (the issue's own core motivation:
"subscribed but can't tell if it stopped working").

Real instances only, per the testing policy: no ``unittest.mock`` /
``MagicMock`` / ``AsyncMock`` / ``patch``. Uses the SAME real low-level MCP
server subprocess ②b's / #3698 PR-2's own tests use
(``tests/_support/mcp_subscribable_resources_server.py``) through a REAL
``MCPConnectionService``, both under Legacy (default protocol_mode) and
under Listen (``protocol_mode="auto"`` against a 2026-07-28+ negotiation)
— see ``test_3698_pr2_subscription_port.py`` for the same two-config
pattern this file borrows.

A real partial-honored (some-but-not-all URIs declined) response was never
observed against any test double in this repo as of #4686 (architect's own
measurement) — ``unhonored_uris``'s "not honored" branch is exercised by
#4686's own falsification below (reverting ``_last_honored`` population)
rather than a fabricated protocol frame, since fabricating one would pin
this repo's OWN test double's behavior, not the MCP server's.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.mcp.connection_service import MCPConnectionService
from tests._support.paths import REPO_ROOT

_SUPPORT_DIR = REPO_ROOT / "tests" / "_support"
_SUBSCRIBABLE_SERVER = _SUPPORT_DIR / "mcp_subscribable_resources_server.py"
_URI = "resource://counter"


def _stdio_cfg(script: Path, *, modern: bool) -> dict:
    cfg: dict = {"type": "stdio", "command": sys.executable, "args": [str(script)]}
    if modern:
        cfg["protocol_mode"] = "auto"
    return cfg


# ── before any connection ──────────────────────────────────────────────────


def test_unhonored_and_mode_are_none_before_any_connection():
    """Tier 2: no adapter has ever been built for a server this service has
    never seen — ``unhonored_uris``/``subscription_mode`` both read as "can't
    say" (None), and ``subscribed_uris`` is empty (nothing tracked)."""
    service = MCPConnectionService()
    assert service.subscribed_uris("srv") == []
    assert service.unhonored_uris("srv") is None
    assert service.subscription_mode("srv") is None
    assert service.subscription_summary() == []


# ── Legacy: honored-ness cannot be reported at all ──────────────────────────


@pytest.mark.asyncio
async def test_legacy_connection_is_unconfirmed_not_not_honored():
    """Tier 2: a real Legacy ``resources/subscribe`` connection (default
    protocol_mode — no ``resources/subscribe`` per-URI ack in that era)
    reports mode="legacy" and unhonored_uris=None — the "can't tell" state,
    distinct from an empty list (which would claim "confirmed, all live").
    subscription_summary() must reflect the SAME three fields for this
    connection, since it is the single producer both the TUI pane and the
    list_mcp_subscriptions tool read (#4686's own "don't split ①②"
    requirement extended to the shared code path)."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER, modern=False))
        await client.subscribe_resource(_URI)

        assert service.subscribed_uris("srv") == [_URI]
        assert service.subscription_mode("srv") == "legacy"
        assert service.unhonored_uris("srv") is None
        assert service.subscription_summary() == [
            {"server": "srv", "mode": "legacy", "uris": [_URI], "unhonored": None},
        ]
    finally:
        await service.aclose()


# ── Listen: honored-ness IS reportable, and this real server honors it ─────


@pytest.mark.asyncio
async def test_listen_connection_reports_confirmed_when_server_honors_the_request():
    """Tier 2: a real modern-negotiated (Listen) connection against a server
    that accepts the subscription reports mode="listen" and
    unhonored_uris=[] — an EMPTY list, not None: this connection CAN report
    honored-ness, and confirms every requested URI was honored. The
    third state (a non-empty unhonored subset) is the one #4686 could not
    observe against any real test double — see the module docstring."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER, modern=True))
        await client.subscribe_resource(_URI)

        assert service.subscribed_uris("srv") == [_URI]
        assert service.subscription_mode("srv") == "listen"
        assert service.unhonored_uris("srv") == []
        assert service.subscription_summary() == [
            {"server": "srv", "mode": "listen", "uris": [_URI], "unhonored": []},
        ]
    finally:
        await service.aclose()


# ── subscription_summary population rule ────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_omits_a_held_server_with_no_subscribed_uris():
    """Tier 2: a HELD connection (server appears in held_servers()) with no
    subscription yet contributes nothing to subscription_summary() — the
    #4686 design's own "nothing this adds over the existing visibility_items
    row" rule (Session.mcp_subscription_state's docstring)."""
    service = MCPConnectionService()
    try:
        await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER, modern=False))
        assert "srv" in service.held_servers()
        assert service.subscription_summary() == []
    finally:
        await service.aclose()
