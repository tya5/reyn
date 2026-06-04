"""Tier 2: http_get host-membership cutover + NETWORK_HOST wildcard (#1199 S3.1b-2c-2).

require_http_get's host-MEMBERSHIP decision (has_specific OR has_wildcard) routes
through the model (NETWORK_HOST axis, decl-membership only — no approval_check, as
the config/persisted/legacy approvals are separate disjuncts). The intricate
resolution flow (config-deny tiers / startup_guard prompt / legacy compat) stays.
The broad byte-identical guard is the http_get/web_fetch suites; these pin the
NETWORK_HOST wildcard + the membership routing (via the bus=None raise paths).
python is deliberately NOT routed (resolves+returns a PythonPermission).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.effective import AgentLayer, CapabilityAxis
from reyn.permissions.permissions import PermissionDecl
from tests.test_permissions import _make_resolver

AX = CapabilityAxis


def test_network_host_wildcard_and_specific() -> None:
    """Tier 2: NETWORK_HOST honors a specific declared host AND the "*" wildcard
    (faithful to require_http_get's has_specific OR has_wildcard)."""
    assert AgentLayer(PermissionDecl(http_get=[{"host": "api.x.com"}])).allows(
        AX.NETWORK_HOST, "api.x.com"
    )
    assert not AgentLayer(PermissionDecl(http_get=[{"host": "api.x.com"}])).allows(
        AX.NETWORK_HOST, "other.com"
    )
    assert AgentLayer(PermissionDecl(http_get=[{"host": "*"}])).allows(
        AX.NETWORK_HOST, "anything.com"
    )  # wildcard


@pytest.mark.asyncio
async def test_require_http_get_membership_routes_via_model(tmp_path: Path) -> None:
    """Tier 2: the membership decision routes through the model — a declared or
    wildcard host reaches the interactive-prompt path (membership True); an
    undeclared host falls to the no-declaration legacy path (membership False).
    Distinguished here via the bus=None raise messages (byte-identical)."""
    r = _make_resolver(tmp_path)  # no config approvals
    decl = PermissionDecl(http_get=[{"host": "api.example.com"}])
    # declared → membership True → needs interactive prompt (bus=None → that path)
    with pytest.raises(PermissionError, match="requires an interactive prompt"):
        await r.require_http_get(decl, "api.example.com", None, "skill")
    # wildcard → membership True → same prompt path
    with pytest.raises(PermissionError, match="requires an interactive prompt"):
        await r.require_http_get(
            PermissionDecl(http_get=[{"host": "*"}]), "any.host.com", None, "skill"
        )
    # undeclared → membership False → no-declaration legacy path (distinct message)
    with pytest.raises(PermissionError, match="not declared and no interactive bus"):
        await r.require_http_get(PermissionDecl(), "api.example.com", None, "skill")
