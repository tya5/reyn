"""Tests for PermissionResolver — focusing on require_mcp (PR37 allowed_mcp).

These tests verify that the per-agent MCP allowlist (PermissionDecl.allowed_mcp)
correctly gates MCP server access independently of the phase-level mcp scope and
the project-wide config approval.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_resolver(tmp_path: Path, *, config: dict | None = None) -> PermissionResolver:
    """Build a non-interactive PermissionResolver backed by tmp_path."""
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=False,
    )


class _AutoDenyInterventionBus:
    """Real InterventionBus that auto-denies every prompt with choice_id="no".

    These tests construct PermissionDecl with config["allow"|"deny"] explicitly,
    so the resolver never reaches the interactive path. The bus is required as
    a constructor argument but is structurally never invoked. Recording calls
    lets a test assert the no-prompt invariant if it wants.
    """

    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id="no")


def _make_bus() -> InterventionBus:
    """Real auto-denying bus (non-interactive tests never trigger it)."""
    return _AutoDenyInterventionBus()


def _run(coro):
    return asyncio.run(coro)


# ── PR37: require_mcp blocked by per-agent allowed_mcp ───────────────────────


def test_require_mcp_blocked_by_allowed_mcp(tmp_path):
    """require_mcp raises when server is in decl.mcp but NOT in allowed_mcp.

    Scenario: project config allows both "fs" and "web"; phase declares both;
    but the agent's allowed_mcp only permits "fs". Calling require_mcp("web")
    must raise PermissionError before the project-config check runs.
    """
    resolver = _make_resolver(
        tmp_path,
        config={"mcp.fs": "allow", "mcp.web": "allow"},
    )
    decl = PermissionDecl(
        mcp=["fs", "web"],
        allowed_mcp=["fs"],   # only "fs" for this agent
    )
    bus = _make_bus()

    # "fs" must pass
    _run(resolver.require_mcp(decl, "fs", bus))

    # "web" is declared and project-approved but blocked by allowed_mcp
    with pytest.raises(PermissionError, match="allowed_mcp"):
        _run(resolver.require_mcp(decl, "web", bus))


def test_require_mcp_allowed_mcp_none_no_filter(tmp_path):
    """allowed_mcp=None means no per-agent filter — only decl.mcp + project config gate."""
    resolver = _make_resolver(
        tmp_path,
        config={"mcp.filesystem": "allow"},
    )
    decl = PermissionDecl(
        mcp=["filesystem"],
        allowed_mcp=None,
    )
    bus = _make_bus()

    # Should pass — no per-agent restriction, server is in decl.mcp and project-approved
    _run(resolver.require_mcp(decl, "filesystem", bus))


def test_require_mcp_still_requires_decl_mcp_even_with_allowed_mcp(tmp_path):
    """allowed_mcp permitting a server doesn't bypass the decl.mcp requirement.

    A server listed in allowed_mcp but NOT in decl.mcp must still fail.
    """
    resolver = _make_resolver(
        tmp_path,
        config={"mcp.filesystem": "allow"},
    )
    decl = PermissionDecl(
        mcp=[],                        # phase did not declare this server
        allowed_mcp=["filesystem"],    # but allowed_mcp includes it
    )
    bus = _make_bus()

    with pytest.raises(PermissionError, match="not declared in skill permissions"):
        _run(resolver.require_mcp(decl, "filesystem", bus))


def test_require_mcp_allowed_mcp_empty_list_blocks_all(tmp_path):
    """allowed_mcp=[] (empty list) blocks every MCP server for the agent."""
    resolver = _make_resolver(
        tmp_path,
        config={"mcp.filesystem": "allow"},
    )
    decl = PermissionDecl(
        mcp=["filesystem"],
        allowed_mcp=[],   # nothing allowed at agent level
    )
    bus = _make_bus()

    with pytest.raises(PermissionError, match="allowed_mcp"):
        _run(resolver.require_mcp(decl, "filesystem", bus))
