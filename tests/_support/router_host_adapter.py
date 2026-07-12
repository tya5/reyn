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


async def null_send_to_agent(*, to, request, depth, chain_id) -> None:
    pass


async def null_put_outbox(msg) -> None:
    pass


def null_append_history(msg) -> None:
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
    task_backend: object = None,  # #1953 / #2107: session-scoped Task backend
    task_waker: object = None,    # #2107: OS TaskWaker (router task.* terminal → requester wake)
    session_id: "str | None" = None,
    current_task_id_fn: "object | None" = None,  # #1953 §16: per-turn execution context
    turn_origin_fn: "object | None" = None,  # proposal 0060 Phase 1 (A7): per-turn provenance source
    workspace_base_dir: "object | None" = None,  # router-op-ctx Workspace root (else cwd)
    agent_registry: object = None,  # #2103: real AgentRegistry for spawn/topology seams
    pipeline_registry: object = None,  # IS-5: real PipelineRegistry for run_pipeline lookup
    on_limit: object = None,  # #2175: OnLimitConfig for the spawn-limit checkpoint (None → no checkpoint = unattended reject)
    safety_extensions: "dict | None" = None,  # #2175: shared per-run extension dict
    intervention_answer: "str | None" = None,  # #2175: interactive-mode bus answer (choice_id, e.g. "yes")
) -> RouterHostAdapter:
    """Construct a minimal RouterHostAdapter with real collaborators."""
    if events is None:
        events = EventLog(subscribers=[])
    # #2175: a REAL on_limit checkpoint (no mock) wrapping handle_limit_exceeded with a
    # real OnLimitConfig + a real approving/declining bus, so spawn-limit tests exercise
    # the actual framework. None on_limit + None answer → no checkpoint wired → the host
    # adapter degrades to unattended (reject) — the C3 hard-deny posture.
    _ext: dict = safety_extensions if safety_extensions is not None else {}
    _checkpoint = None
    if on_limit is not None:
        from reyn.runtime.limits.limit_handler import handle_limit_exceeded

        class _FixedAnswerBus:  # real callable bus (not a mock)
            async def request(self, iv):  # noqa: ANN001
                from reyn.user_intervention import InterventionAnswer
                return InterventionAnswer(choice_id=intervention_answer)

        _bus = _FixedAnswerBus() if intervention_answer is not None else None

        async def _checkpoint_fn(*, kind, prompt, detail, extension_amount, run_id=None):
            decision = await handle_limit_exceeded(
                bus=_bus, on_limit=on_limit, kind=kind, run_id=run_id or "test",
                prompt=prompt, detail=detail, extension_amount=extension_amount,
            )
            if decision.allow_continue:
                _ext[kind] = _ext.get(kind, 0.0) + decision.extension
            return decision

        _checkpoint = _checkpoint_fn
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
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=resolver,
        memory=memory,
        journal=None,
        agent_registry=agent_registry,
        pipeline_registry=pipeline_registry,  # IS-5
        handle_chat_limit_checkpoint=_checkpoint,  # #2175
        safety_extensions=_ext,  # #2175
        agent_workspace_dir=workspace,
        file_read=null_file_read,
        file_write=null_file_write,
        file_delete=null_file_delete,
        file_list_directory=null_file_list,
        file_regenerate_index=null_file_regen,
        mcp_list_servers=null_mcp_list_servers,
        mcp_list_tools=null_mcp_list_tools,
        mcp_call_tool=null_mcp_call_tool,
        send_to_agent=null_send_to_agent,
        put_outbox=null_put_outbox,
        append_history=null_append_history,
        delegation_tracker=lambda: _delegations,
        agent_replies_tracker=lambda: _replies,
        turn_budget_engine=turn_budget_engine,
        environment_backend=environment_backend,
        task_backend=task_backend,
        task_waker=task_waker,
        session_id=session_id,
        current_task_id_fn=current_task_id_fn,
        turn_origin_fn=turn_origin_fn,
        workspace_base_dir=workspace_base_dir,
    )
