"""Tier 2 invariant tests for RouterHostAdapter (wave 3 PR3).

Verifies structural properties of RouterHostAdapter without using mocks of
collaborators. Real services (MemoryService, EventLog) and closure-based
callbacks are used throughout. No private state assertions — public surface only.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.router_loop import RouterLoopHost
from reyn.chat.services import MemoryService, RouterHostAdapter
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver

# ---------------------------------------------------------------------------
# Minimal stubs and helpers
# ---------------------------------------------------------------------------

class _FakeEventStore:
    """Minimal event store that discards events."""

    def emit(self, type: str, **data) -> None:
        pass


class _FakePermResolver:
    """Stub PermissionResolver with no configured permissions."""
    _config: dict = {}


def _null_async(*args, **kwargs):
    async def _inner(*a, **kw):
        return {}
    return _inner()


async def _null_file_read(path: str) -> dict:
    return {"content": ""}


async def _null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def _null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def _null_file_list(path: str) -> dict:
    return {"path": path, "entries": []}


async def _null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def _null_mcp_list_servers() -> list:
    return []


async def _null_mcp_list_tools(server: str) -> list:
    return []


async def _null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
    return {}


async def _null_run_skill(spec, *, chain_id) -> dict:
    return {"status": "finished", "data": {}}


async def _null_spawn_skill(spec, *, chain_id) -> dict:
    """FP-0012: spawn-ack stub for adapter tests."""
    return {
        "status": "spawned",
        "run_id": "test-run-id",
        "chain_id": chain_id,
        "skill": spec.get("skill", ""),
        "note": "test stub",
    }


async def _null_send_to_agent(*, to, request, depth, chain_id) -> None:
    pass


async def _null_put_outbox(msg) -> None:
    pass


def _null_append_history(msg) -> None:
    pass


async def _null_spawn_plan_task(*, plan_id, runtime, chain_id, parent_chain_id=None) -> None:
    pass


def _make_adapter(
    agent_name: str = "test-agent",
    agent_workspace_dir: Path | None = None,
    events: EventLog | None = None,
    memory: MemoryService | None = None,
    delegation_list: "list[dict] | None" = None,
    agent_replies_list: "list[str] | None" = None,
    resolver: ModelResolver | None = None,
) -> RouterHostAdapter:
    """Construct a minimal RouterHostAdapter with real collaborators."""
    if events is None:
        events = EventLog(subscribers=[])
    workspace = agent_workspace_dir or Path(".reyn") / "agents" / agent_name
    if memory is None:
        memory = MemoryService(
            agent_workspace_dir=workspace,
            events=events,
            file_write=_null_file_write,
            file_read=_null_file_read,
            file_delete=_null_file_delete,
            file_regenerate_index=_null_file_regen,
        )
    if resolver is None:
        resolver = ModelResolver({})

    _delegations = delegation_list
    _replies = agent_replies_list

    return RouterHostAdapter(
        agent_name=agent_name,
        agent_role="test role",
        output_language="en",
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=resolver,
        memory=memory,
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace,
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=_null_mcp_list_tools,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: _delegations,
        agent_replies_tracker=lambda: _replies,
    )


# ---------------------------------------------------------------------------
# Test 1: Protocol conformance (runtime_checkable isinstance)
# ---------------------------------------------------------------------------

def test_adapter_protocol_conformance(tmp_path):
    """Tier 2: RouterHostAdapter is structurally conformant with RouterLoopHost.

    Uses @runtime_checkable isinstance check — catches missing methods at
    refactor time without requiring a real LLM or full session.
    """
    adapter = _make_adapter(agent_workspace_dir=tmp_path / "agents" / "test-agent")
    assert isinstance(adapter, RouterLoopHost), (
        "RouterHostAdapter must satisfy RouterLoopHost protocol structurally"
    )


# ---------------------------------------------------------------------------
# Test 2: memory_path / memory_dir delegation
# ---------------------------------------------------------------------------

def test_memory_path_delegation_matches_service(tmp_path):
    """Tier 2: adapter.memory_path returns the same value as MemoryService.memory_path.

    Asserts no double-mapping or path transformation between the adapter
    delegation surface and the service's own method.
    """
    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "test-agent"
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = _make_adapter(
        agent_workspace_dir=workspace,
        events=events,
        memory=memory,
    )

    assert adapter.memory_path("shared", "test_slug") == memory.memory_path("shared", "test_slug")
    assert adapter.memory_dir("agent") == memory.memory_dir("agent")


# ---------------------------------------------------------------------------
# Test 3: events identity
# ---------------------------------------------------------------------------

def test_events_identity(tmp_path):
    """Tier 2: adapter.events is the same EventLog object as the session's _chat_events.

    No duplicate event log surface — ensures there is a single append-only
    event log for the session (P6 compliance).
    """
    events = EventLog(subscribers=[])
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test-agent",
        events=events,
    )
    assert adapter.events is events, (
        "adapter.events must be the same EventLog instance as injected"
    )


# ---------------------------------------------------------------------------
# Test 4: delegation tracker via callback
# ---------------------------------------------------------------------------

def test_delegation_tracker_appended_on_send_to_agent(tmp_path):
    """Tier 2: send_to_agent appends to the delegation tracker list supplied via callback.

    When the delegation_tracker callback returns a mutable list, calling
    send_to_agent must append a dict with 'to' and 'request' keys.
    """
    tracker: list[dict] = []
    calls: list[dict] = []

    async def fake_send(*, to, request, depth, chain_id) -> None:
        calls.append({"to": to, "request": request})

    adapter = RouterHostAdapter(
        agent_name="alpha",
        agent_role="role",
        output_language=None,
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=EventLog(subscribers=[]),
        resolver=ModelResolver({}),
        memory=MemoryService(
            agent_workspace_dir=tmp_path / "agents" / "alpha",
            events=EventLog(subscribers=[]),
            file_write=_null_file_write,
            file_read=_null_file_read,
            file_delete=_null_file_delete,
            file_regenerate_index=_null_file_regen,
        ),
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=tmp_path / "agents" / "alpha",
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=_null_mcp_list_tools,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=fake_send,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: tracker,
        agent_replies_tracker=lambda: None,
    )

    asyncio.run(adapter.send_to_agent(
        to="beta", request="hello from alpha", depth=1, chain_id="chain-x",
    ))

    (entry,) = tracker
    assert entry["to"] == "beta"
    assert entry["request"] == "hello from alpha"


# ---------------------------------------------------------------------------
# #53 regression — permission_resolver property + intervention_bus wiring
# ---------------------------------------------------------------------------

def test_adapter_exposes_permission_resolver_property(tmp_path):
    """Tier 2: adapter.permission_resolver returns the resolver from __init__.

    Regression for #53. RouterLoop builds the ToolContext via
    ``getattr(self.host, "permission_resolver", None)``. Before the fix
    this returned None (the adapter stored the resolver as ``_perm`` only),
    so every router-invoked tool's permission_resolver was silently None
    and Tier-1 config-deny checks (web.fetch, mcp, …) were bypassed.

    The property must mirror what was passed to ``permission_resolver=``
    at construction time so the getattr lookup wires the right object.
    """
    sentinel = object()  # any non-None value — we only assert identity
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "alpha",
    )
    # Re-build with the resolver argument set — _make_adapter doesn't take it.
    from reyn.chat.services import MemoryService
    from reyn.chat.services.router_host_adapter import RouterHostAdapter
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    workspace = tmp_path / "agents" / "alpha2"
    events = EventLog(subscribers=[])
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = RouterHostAdapter(
        agent_name="alpha2",
        agent_role="role",
        output_language=None,
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=sentinel,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace,
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=_null_mcp_list_tools,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
    )

    assert adapter.permission_resolver is sentinel, (
        "adapter.permission_resolver must mirror the __init__ argument so "
        "RouterLoop's ToolContext.permission_resolver getattr lookup wires "
        "the session's resolver into router-invoked tool dispatch (#53)."
    )


def test_make_router_op_context_wires_intervention_bus(tmp_path):
    """Tier 2: make_router_op_context populates ``ctx.intervention_bus``
    via the ``intervention_bus_factory`` callable when provided.

    Regression for #53. web_fetch / mcp install / mcp drop handlers all
    guard ``if ctx.intervention_bus is None`` and raise RuntimeError
    when missing. Without this wiring, even a properly-resolved
    permission_resolver crashes the router path before it can deny.
    """
    from reyn.chat.services import MemoryService
    from reyn.chat.services.router_host_adapter import RouterHostAdapter
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    workspace = tmp_path / "agents" / "bus-test"
    events = EventLog(subscribers=[])
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    sentinel_bus = object()  # we only assert identity, not protocol
    adapter = RouterHostAdapter(
        agent_name="bus-test",
        agent_role="role",
        output_language=None,
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace,
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=_null_mcp_list_tools,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
        intervention_bus_factory=lambda: sentinel_bus,
    )

    op_ctx = adapter.make_router_op_context()
    assert op_ctx.intervention_bus is sentinel_bus, (
        "make_router_op_context must call intervention_bus_factory() and "
        "wire the result into ctx.intervention_bus (#53)."
    )


def test_make_router_op_context_no_factory_leaves_bus_none(tmp_path):
    """Tier 2: factory-not-provided path keeps intervention_bus=None.

    Backward-compat sibling to the wiring test — narrow test sites that
    don't pass intervention_bus_factory must get the old behaviour
    (None bus), so the config-deny path still works without forcing
    every adapter caller to wire a bus.
    """
    from reyn.chat.services import MemoryService
    from reyn.chat.services.router_host_adapter import RouterHostAdapter
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    workspace = tmp_path / "agents" / "nobus-test"
    events = EventLog(subscribers=[])
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = RouterHostAdapter(
        agent_name="nobus-test",
        agent_role="role",
        output_language=None,
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace,
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=_null_mcp_list_tools,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
    )

    op_ctx = adapter.make_router_op_context()
    assert op_ctx.intervention_bus is None
