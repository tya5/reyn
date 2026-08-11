"""Tier 2: ``list_agents_impl`` (src/reyn/mcp/server.py) has a real consumer test.

lead-coder's #4189 finding: ``list_agents_impl`` is live production code — the
backing implementation of the MCP ``list_agents`` tool, called from
``_call_tool`` at ``src/reyn/mcp/server.py:487`` — but had ZERO tests actually
calling it. Its paired ``send_to_agent_impl`` IS called directly from 4 test
files (real-consumer coverage); a stale module docstring in one of those files
claimed "both covered" (corrected in #4188).

Grep trap: ``"list_agents"`` matches heavily, but almost every hit is
``reyn.tools.catalog.LIST_AGENTS`` (``src/reyn/tools/catalog.py:69``) — the
UNRELATED router-dispatch ``list_agents`` action, well-tested on its own path.
That coverage made this module's own ``list_agents_impl`` gap invisible to a
name grep.

This file calls ``list_agents_impl`` DIRECTLY with a real ``AgentRegistry``
(the same precedent ``test_3595_step1b_external_producer_slash_reachability.py``
sets for ``send_to_agent_impl`` — a real production consumer, not
``monkeypatch.setattr`` substituting a fake in its place, which is the shape
the existing 4 files near this surface all use and none of them touch the
real implementation).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.mcp.server import list_agents_impl
from reyn.runtime.registry import AgentRegistry


def _no_factory(profile):
    raise RuntimeError("session factory not used by list_agents_impl")


@pytest.mark.asyncio
async def test_list_agents_impl_returns_name_and_role_excerpt(tmp_path: Path) -> None:
    """Tier 2: the real function, given a real registry with 2 agents, returns
    each agent's name + the first line of its role, stripped — the exact
    shape ``_call_tool`` JSON-serializes back to the MCP client."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    reg.create("alpha", role="coordinator\nsecond line ignored")
    reg.create("beta", role="worker")

    agents = await list_agents_impl(reg)

    by_name = {a["name"]: a["role"] for a in agents}
    assert by_name["alpha"] == "coordinator"
    assert by_name["beta"] == "worker"


@pytest.mark.asyncio
async def test_list_agents_impl_hides_archived_agents(tmp_path: Path) -> None:
    """Tier 2: #1954 — an archived agent is excluded, consistent with every
    other ``list_active_names`` consumer (delegation routing, A2A, the TUI
    Agents tab, ``reyn agent list``'s own default — see
    tests/cli/test_agent_list_hides_archived.py). Without a real-consumer
    test here, this MCP surface could silently diverge from that invariant
    and nothing would catch it."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    reg.create("alpha", role="coordinator")
    reg.create("beta", role="worker")
    reg.remove("beta")  # archive (soft-delete)

    agents = await list_agents_impl(reg)

    names = {a["name"] for a in agents}
    assert "alpha" in names
    assert "beta" not in names


@pytest.mark.asyncio
async def test_list_agents_impl_lists_the_auto_created_default_agent(tmp_path: Path) -> None:
    """Tier 2: a freshly constructed registry is never truly empty —
    ``AgentRegistry`` auto-creates ``DEFAULT_AGENT_NAME`` ("default") — so a
    brand-new project's ``reyn:list_agents()`` call returns that one entry,
    not an error and not an empty list."""
    from reyn.runtime.registry import DEFAULT_AGENT_NAME

    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)

    agents = await list_agents_impl(reg)

    assert [a["name"] for a in agents] == [DEFAULT_AGENT_NAME]
