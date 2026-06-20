"""Shared RouterHostAdapter test builder with real collaborators (no mocks).

Real services (MemoryService, EventLog) and closure-based callbacks are used
throughout. The ``null_*`` callables are inert async stubs (real callables, not
mocks) used as the adapter's action ports when a test only needs construction.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.services import MemoryService, RouterHostAdapter


async def null_file_read(path: str) -> dict:
    return {"content": ""}


async def null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def null_file_list(path: str) -> dict:
    return {"path": path, "entries": []}


async def null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def null_mcp_list_servers() -> list:
    return []


async def null_mcp_list_tools(server: str) -> list:
    return []


async def null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
    return {}


async def null_run_skill(spec, *, chain_id) -> dict:
    return {"status": "finished", "data": {}}


async def null_spawn_skill(spec, *, chain_id) -> dict:
    """FP-0012: spawn-ack stub for adapter tests."""
    return {
        "status": "spawned",
        "run_id": "test-run-id",
        "chain_id": chain_id,
        "skill": spec.get("skill", ""),
        "note": "test stub",
    }


async def null_send_to_agent(*, to, request, depth, chain_id) -> None:
    pass


async def null_put_outbox(msg) -> None:
    pass


def null_append_history(msg) -> None:
    pass


async def null_spawn_plan_task(*, plan_id, runtime, chain_id, parent_chain_id=None) -> None:
    pass


def make_adapter(
    agent_name: str = "test-agent",
    agent_workspace_dir: Path | None = None,
    events: EventLog | None = None,
    memory: MemoryService | None = None,
    delegation_list: "list[dict] | None" = None,
    agent_replies_list: "list[str] | None" = None,
    resolver: ModelResolver | None = None,
    turn_budget_engine: object = None,
    environment_backend: object = None,  # #1477: optional for sandbox-cwd tests
) -> RouterHostAdapter:
    """Construct a minimal RouterHostAdapter with real collaborators."""
    if events is None:
        events = EventLog(subscribers=[])
    workspace = agent_workspace_dir or Path(".reyn") / "agents" / agent_name
    if memory is None:
        memory = MemoryService(
            agent_workspace_dir=workspace,
            events=events,
            file_write=null_file_write,
            file_read=null_file_read,
            file_delete=null_file_delete,
            file_regenerate_index=null_file_regen,
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
        file_read=null_file_read,
        file_write=null_file_write,
        file_delete=null_file_delete,
        file_list_directory=null_file_list,
        file_regenerate_index=null_file_regen,
        mcp_list_servers=null_mcp_list_servers,
        mcp_list_tools=null_mcp_list_tools,
        mcp_call_tool=null_mcp_call_tool,
        run_skill_awaitable=null_run_skill,
        spawn_skill=null_spawn_skill,
        send_to_agent=null_send_to_agent,
        put_outbox=null_put_outbox,
        append_history=null_append_history,
        spawn_plan_task=null_spawn_plan_task,
        delegation_tracker=lambda: _delegations,
        agent_replies_tracker=lambda: _replies,
        turn_budget_engine=turn_budget_engine,
        environment_backend=environment_backend,
    )
