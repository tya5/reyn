"""Tier 2: #5280 — a FAILED reconnect used to leave ``Session.mcp_
subscription_state()``'s reactive cache (#5276/#5279) silently stale.

Root cause: ``MCPConnectionService._reconnect`` pops the dead client from
``self._clients`` FIRST, then attempts ``self._ensure_open(...)`` to reopen.
If the reopen itself raises (the server subprocess is genuinely gone, not
just a transient transport death), the server has ALREADY dropped out of
``held_servers()`` — but ``mcp_initialized`` (the only one of the reactive
cache's original 6 subscribed audit-event kinds that fires on a (re)connect)
only fires on a SUCCESSFUL reopen. So this ONE path left the cache stale
until an unrelated event happened to invalidate it.

Unlike #5278 (a pre-existing gap): before #5276/#5279 landed,
``subscription_summary()`` ran fresh every render frame, so this exact
staleness did not exist before those PRs introduced the cache. This is a
regression THOSE PRs introduced, not a pre-existing one.

Fix: ``_reconnect`` now emits a dedicated ``mcp_reconnect_failed`` audit-
event (via ``self._emit_sink``) when the reopen attempt raises, then
re-raises unchanged — the ONE place both call paths that can reach a
reconnect (``_HeldConnection._heal``'s reactive path and
``_reconnect_from_lost_subscription``'s proactive one) funnel through, so
one fix covers both. ``Session.__init__`` adds this kind to the reactive
cache's subscriber list (session.py).

This file drives the REAL ``MCPConnectionService._reconnect`` against a
config that is guaranteed to fail to (re)open (an invalid stdio command) —
no mocks — and confirms the event actually fires with the right server
name. The companion cache-mechanism test (that this kind, once emitted,
marks ``Session``'s own cache dirty) lives in
``tests/runtime/test_5276_mcp_subscription_reactive_cache.py``, mirroring
that file's own established idiom for the other 6 kinds.
"""
from __future__ import annotations

import pytest

from reyn.mcp.connection_service import MCPConnectionService

_UNREACHABLE_CFG = {
    "type": "stdio",
    "command": "definitely-not-a-real-binary-5280",
    "args": [],
}


@pytest.mark.asyncio
async def test_reconnect_failure_emits_mcp_reconnect_failed() -> None:
    """Tier 2: acceptance — a real ``_reconnect`` call whose reopen attempt
    genuinely fails (an unreachable stdio command) emits
    ``mcp_reconnect_failed`` with the server name, through the real
    ``emit_sink`` wiring — not a private-state peek."""
    events: "list[tuple[str, dict]]" = []

    def _sink(kind: str, **data) -> None:
        events.append((kind, data))

    service = MCPConnectionService(emit_sink=_sink)
    try:
        with pytest.raises(Exception):  # noqa: B017 — the real failure shape varies by platform
            await service._reconnect("srv-5280", _UNREACHABLE_CFG, agent_id=None)

        kinds = [k for k, _ in events]
        assert "mcp_reconnect_failed" in kinds, (
            "#5280 REGRESSION: a failed reconnect emitted nothing — the "
            f"reactive cache has no signal to invalidate on. Emitted kinds: {kinds!r}"
        )
        _kind, data = next(e for e in events if e[0] == "mcp_reconnect_failed")
        assert data.get("server") == "srv-5280", (
            f"expected the failed server's own name on the event, got {data!r}"
        )
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_a_successful_reconnect_does_not_emit_the_failure_kind() -> None:
    """Tier 2: falsification contrast — a real, SUCCESSFUL reconnect (no
    prior held client — the ``old is None`` path, the simplest case that
    still reaches ``_ensure_open``) must never emit ``mcp_reconnect_failed``,
    only the pre-existing ``mcp_initialized``."""
    import sys

    from tests._support.paths import REPO_ROOT

    echo_server = REPO_ROOT / "tests" / "_support" / "mcp_fastmcp_echo_server.py"
    good_cfg = {"type": "stdio", "command": sys.executable, "args": [str(echo_server)]}

    events: "list[tuple[str, dict]]" = []

    def _sink(kind: str, **data) -> None:
        events.append((kind, data))

    service = MCPConnectionService(emit_sink=_sink)
    try:
        client = await service._reconnect("srv-ok-5280", good_cfg, agent_id=None)
        assert client is not None  # sanity: the real reopen succeeded

        kinds = [k for k, _ in events]
        assert "mcp_reconnect_failed" not in kinds, (
            f"a successful reconnect must not emit the failure kind, got {kinds!r}"
        )
        assert "mcp_initialized" in kinds, (
            f"a successful reconnect must still emit the pre-existing kind, got {kinds!r}"
        )
    finally:
        await service.aclose()
