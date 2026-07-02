"""Tier 2: describe_action returns per-resource description (B42-NF-W7-1).

Companion to ``test_describe_action_resource_schemas.py`` (= D2-full
input_schema fix). The D2-full work covered ``input_schema`` only and
left the ``description`` field defaulting to the dispatcher's generic
description (= "Run a skill from the registered list. The 'name'
parameter MUST be..." for ``invoke_skill`` etc.).

The B42 W7-S2 trace ("Tell me more about the simplest one") confirmed
the empty-stop mechanism: when describe_action returns the dispatcher's
generic description instead of the resource's actual one, the LLM has
nothing meaningful to relay and produces an empty reply. N=10
trace-patch-replay: 9/10 empty → 0/10 empty after substituting the
actual ``skill.md`` description.

These tests pin:

- ``skill__X``      → description from ``list_available_skills`` entry.
- ``agent.peer__X`` → description (or role fallback) from
                      ``list_available_agents`` entry.
- ``mcp.tool__X.Y`` → per-tool description from ``mcp_servers``.
- ``mcp.server__X`` → server-level description from ``mcp_servers``.
- Operation categories fall through to ``target.description`` unchanged.
- Missing per-resource description falls through to ``target.description``.
"""
from __future__ import annotations

import asyncio

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _handle_describe_action


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


class _FakeHost:
    """Minimal host returning enriched skill / agent lists."""

    def __init__(self, skills=None, agents=None):
        self._skills = skills or []
        self._agents = agents or []

    def list_available_skills(self):
        return list(self._skills)

    def list_available_agents(self):
        return list(self._agents)


def _make_ctx(skills=None, agents=None, mcp_servers=None):
    rs = RouterCallerState(
        host=_FakeHost(skills=skills, agents=agents),
        mcp_servers=mcp_servers,
    )
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


def _describe(qualified_name: str, ctx: ToolContext) -> dict:
    return asyncio.run(_handle_describe_action(
        {"action_name": qualified_name}, ctx,
    ))


# Issue #879: mcp.tool / mcp.server resource-invoke describe paths were
# removed when the MCP surface collapsed to verb actions. The per-tool /
# per-server description metadata is now surfaced through
# mcp__list_tools / mcp__list_servers results directly instead of
# describe_action; that flow's coverage lives in test_universal_handlers
# (= LIST_MCP_TOOLS / LIST_MCP_SERVERS handler tests).
#
# Phase 1 multi_agent collapse (2026-05-25): same pattern applied to the
# agent.peer__X resource shape. Per-peer description surfaces through
# multi_agent__describe_peer / multi_agent__list_peers results; coverage
# lives in test_universal_handlers (= DESCRIBE_AGENT / LIST_AGENTS).


# ── operation categories (regression guard) ─────────────────────────────


def test_operation_describe_unchanged_by_resource_description_fix():
    """Tier 2b: operation-category actions continue to use target.description.

    Regression guard: file__read, web__fetch, etc. must not be affected by
    the B42-NF-W7-1 resource-description fix — their resolution path is
    unchanged.
    """
    ctx = _make_ctx()
    out = _describe("file__read", ctx)
    # The file__read tool description must be non-empty and is the
    # operation's own description (not a resource-category fallback).
    assert out["description"]
    # And it shouldn't accidentally pull from the (empty) skills list.
    assert "Catalogue-gap fallback" not in out["description"]
