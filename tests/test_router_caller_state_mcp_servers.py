"""Tier 2: RouterCallerState.mcp_servers is populated from host.get_mcp_servers().

Pre-fix, ``RouterLoop._build_router_caller_state`` constructed a
``RouterCallerState`` without setting ``mcp_servers=``. The dataclass
defaulted the field to ``None``, so ``universal_catalog._enumerate_category``
for the ``mcp.server`` and ``mcp.tool`` categories returned ``[]``
even when ``reyn mcp list`` showed configured servers and
``host.get_mcp_servers()`` returned them. The data was wired into
``build_tools(mcp_servers=...)`` for the tools[] catalog but never
into the ``RouterCallerState`` slot the ``list_actions`` enumerator
consumes.

This test pins the wiring: when the host returns servers,
``RouterCallerState.mcp_servers`` matches that list, and
``list_actions(category=["mcp.server"])`` surfaces the qualified
names.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _FakeHost:
    """Minimal RouterLoopHost stub exposing only get_mcp_servers + bits
    ``_build_router_caller_state`` reads. Real shape, no mock framework."""

    agent_name: str = "test-agent"
    agent_role: str = ""
    output_language: str = "en"

    def __init__(self, servers: list[dict]) -> None:
        self._servers = servers

        class _E:
            def emit(self, *a, **kw): pass
            subscribers: list = []
        self._events = _E()

    @property
    def events(self): return self._events

    def get_universal_wrappers_enabled(self) -> bool: return True
    def get_action_usage_tracker(self): return None
    def get_action_embedding_index(self): return None
    def get_embedding_provider(self): return None
    def get_embedding_model_class(self): return None
    def get_action_retrieval_config(self): return None
    def list_available_skills(self) -> list[dict]: return []
    def list_available_agents(self) -> list[dict]: return []
    def get_memory_index(self) -> dict: return {"status": "not_found", "content": ""}
    def get_file_permissions(self): return None
    def get_mcp_servers(self) -> list[dict]: return self._servers
    def get_web_fetch_allowed(self) -> bool: return False
    def get_project_context(self) -> str: return ""
    def get_sandbox_backend(self): return None
    def resolve_model(self, name: str) -> str: return "fake-model"


def _build_router_loop(host: _FakeHost) -> Any:
    from reyn.chat.router_loop import RouterLoop
    return RouterLoop(host=host, chain_id="c1", router_model="standard")


def test_mcp_servers_threaded_into_router_caller_state() -> None:
    """Tier 2: host.get_mcp_servers() lands on rs.mcp_servers.

    The pre-fix bug: ``RouterCallerState(...)`` construction omitted
    the ``mcp_servers=`` argument, so the dataclass default (``None``)
    won and ``_enumerate_category("mcp.server", ctx)`` returned ``[]``.
    """
    servers = [
        {"name": "brave", "description": "web search"},
        {"name": "filesystem", "description": "fs"},
    ]
    loop = _build_router_loop(_FakeHost(servers))
    rs = asyncio.run(loop._build_router_caller_state())
    assert rs.mcp_servers == servers, (
        f"expected rs.mcp_servers == {servers!r}, got {rs.mcp_servers!r}"
    )


def test_mcp_servers_empty_host_lands_as_empty_list() -> None:
    """Tier 2: empty host.get_mcp_servers() → rs.mcp_servers == [].

    Distinct from the bug state (= rs.mcp_servers is None). Empty list
    keeps the field truthy at the right structural shape so downstream
    iteration (``for s in rs.mcp_servers``) is a no-op rather than
    ``TypeError: 'NoneType' is not iterable``.
    """
    loop = _build_router_loop(_FakeHost([]))
    rs = asyncio.run(loop._build_router_caller_state())
    assert rs.mcp_servers == []


def test_mcp_servers_missing_method_falls_back_to_none() -> None:
    """Tier 2: a host without get_mcp_servers() → rs.mcp_servers is None.

    Backward-compat for narrow hosts (FakeRouterHost variants, plan-step
    host) that legitimately don't expose MCP state. ``hasattr`` guard
    keeps the field None rather than raising AttributeError.

    Uses a standalone class (= NOT a subclass of _FakeHost) so removing
    methods doesn't leak across tests via class-level mutation.
    """
    class _Slim:
        agent_name = "x"; agent_role = ""; output_language = "en"
        class _E:
            def emit(self, *a, **kw): pass
            subscribers: list = []
        events = _E()
        def get_universal_wrappers_enabled(self): return True
        def get_action_usage_tracker(self): return None
        def get_action_embedding_index(self): return None
        def get_embedding_provider(self): return None
        def get_embedding_model_class(self): return None
        def get_action_retrieval_config(self): return None
        def list_available_skills(self): return []
        def list_available_agents(self): return []
        def get_memory_index(self): return {"status": "not_found", "content": ""}
        def get_file_permissions(self): return None
        def get_web_fetch_allowed(self): return False
        def get_project_context(self): return ""
        def get_sandbox_backend(self): return None
        def resolve_model(self, name): return "fake-model"
        # Deliberately NO get_mcp_servers method.

    from reyn.chat.router_loop import RouterLoop
    loop = RouterLoop(host=_Slim(), chain_id="c1", router_model="standard")
    rs = asyncio.run(loop._build_router_caller_state())
    assert rs.mcp_servers is None


@pytest.mark.asyncio
async def test_list_actions_mcp_server_surfaces_servers_e2e() -> None:
    """Tier 2: end-to-end — list_actions(category=["mcp.server"]) returns
    qualified names for every host-configured server.

    This is the user-observed symptom: ``reyn mcp list`` shows servers,
    but ``list_actions(category="mcp.server")`` returned []. Pin the
    fix at the enumerator boundary too.
    """
    from reyn.chat.router_loop import RouterLoop
    from reyn.tools.types import ToolContext
    from reyn.tools.universal_catalog import LIST_ACTIONS

    servers = [
        {"name": "brave", "description": "web search"},
        {"name": "github", "description": "GitHub"},
    ]
    loop = _build_router_loop(_FakeHost(servers))
    rs = await loop._build_router_caller_state()
    ctx = ToolContext(
        events=loop.host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )
    result = await LIST_ACTIONS.handler({"category": ["mcp.server"]}, ctx)
    qns = {it["qualified_name"] for it in result["items"]}
    assert qns == {"mcp.server__brave", "mcp.server__github"}
