"""Shared RouterHostAdapter + probe helpers for MCP tools-cache tests.

``CountingProbe`` is a real async callable that records which servers a
``mcp_list_tools`` probe was invoked for; ``make_mcp_cache_adapter`` builds a
``RouterHostAdapter`` wired to it, with every other collaborator a no-op via
``tests._support.router_host_adapter``'s shared null callbacks.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.services import (
    LiveSessionIdInputs,
    McpGatewayInputs,
    MemoryService,
    PutOutboxInputs,
    RouterHostAdapter,
    SendToAgentInputs,
)
from tests._support.router_host_adapter import (
    make_op_context_source,
    null_append_history,
    null_file_delete,
    null_file_read,
    null_file_regen,
    null_file_write,
    null_mcp_call_tool,
    null_put_outbox,
    null_send_to_agent,
)

_EMPTY_OP_CTX = make_op_context_source()
_EMPTY_MCP_GATEWAY = McpGatewayInputs(
    mcp_connection_service=None, mcp_agent_id=None, ephemeral_fn=None,
)


class CountingProbe:
    """Real async callable that records which servers were probed."""

    def __init__(self, tools_by_server: dict[str, list[dict]] | None = None) -> None:
        self.calls: list[str] = []
        self._tools = tools_by_server or {}

    async def __call__(self, server_name: str) -> list[dict]:
        self.calls.append(server_name)
        return list(self._tools.get(server_name, []))


def make_mcp_cache_adapter(
    *,
    tmp_path: Path,
    mcp_servers: dict | None,
    probe: CountingProbe,
    state_dir: Path,
) -> RouterHostAdapter:
    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "test-agent"
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=null_file_write,
        file_read=null_file_read,
        file_delete=null_file_delete,
        file_regenerate_index=null_file_regen,
    )
    adapter = RouterHostAdapter(
        agent_name="test-agent",
        agent_role="test",
        output_language="en",
        op_context_source=_EMPTY_OP_CTX,
        permission_resolver=None,
        mcp_servers=mcp_servers,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        send_to_agent_inputs=SendToAgentInputs(
            send_to_agent=null_send_to_agent, delegation_tracker=lambda: None,
        ),
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        state_dir=state_dir,
    )
    # #3447: mcp_list_tools is now a real RouterHostAdapter method — see the
    # same-shaped note in test_mcp_lazy_tools_cache.py's _make_adapter_with_mcp.
    adapter.mcp_list_tools = probe
    return adapter
