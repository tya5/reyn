"""Tier 2: owner-reported bug — list_mcp_servers/list_mcp_tools reported "0" to the
LLM while the conversation transcript correctly showed the real count.

Root cause: ``RouterLoop._normalise_router_tool_result`` (router_loop.py) unwrapped
the handler's ``{"servers": [...]}`` / ``{"mcp_tools": [...]}`` dict down to a bare
list — a legacy "LLM-visible-identical to the pre-registry-dispatch path" shim that
predates the canonicalization system. ``dispatch_tool``'s envelope then wrapped a
bare list as ``data``; ``unwrap_dispatch_envelope`` only peels a dict-shaped
``data``, so it stayed wrapped, and ``list_mcp_servers_to_canonical``'s
``result.get("servers")`` found nothing — reporting "0 MCP servers." to the LLM,
while the conversation's raw-dict summary (which tolerates a bare list, showing
"N items") displayed the real count. Restart-invariant: a pure shape bug, not state.

Verified end-to-end (real Session, real event bus, real ChatLifecycleForwarder,
real RouterLoop, real dispatch_tool — not hand-built envelope data) before writing
this fix, per the owner's explicit push to confirm the two "sources" (conversation
display vs LLM-facing text) actually derive from the literal same call.
"""
from __future__ import annotations

import asyncio
import functools
from pathlib import Path

from reyn.config.loader import load_config
from reyn.core.dispatch.dispatcher import DispatchContext, dispatch_tool
from reyn.core.events.state_log import StateLog
from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.session import Session
from reyn.tools import get_default_registry
from reyn.tools.scheme import ExecutionResult
from tests._support.agent_session import make_session


def _session(tmp_path: Path) -> Session:
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    (tmp_path / "reyn.local.yaml").write_text(
        "mcp:\n  servers:\n"
        "    s1:\n      command: /usr/bin/true\n      description: server 1\n"
        "    s2:\n      command: /usr/bin/true\n      description: server 2\n",
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path)
    return make_session(
        agent_name="alice",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        mcp_servers=config.mcp,
    )


def test_end_to_end_list_mcp_servers_matches_conversation_and_llm_text(tmp_path) -> None:
    """Tier 2: falsifying, end-to-end — real Session, real event bus, real
    ChatLifecycleForwarder (conversation display source), real RouterLoop.feedback
    (LLM-facing text source), fed by ONE real dispatch_tool() call (not hand-built
    envelope data). Both consumers must report the SAME server count.

    Pre-fix: the conversation showed "2 items" (or similar bare-list rendering)
    while the LLM-facing text showed "0 MCP servers." — this test's assertions on
    the LLM text would fail against the pre-fix code."""
    get_default_registry()
    session = _session(tmp_path)

    outbox = session.outbox
    forwarder = ChatLifecycleForwarder(outbox)
    session._chat_events.add_subscriber(forwarder)

    loop = RouterLoop(host=session._router_host, chain_id="c1", router_model="gpt-4o")
    catalog = {"list_mcp_servers": {"function": {"name": "list_mcp_servers", "parameters": {}}}}
    ctx = DispatchContext(
        caller_kind="router", caller_id="alice", chain_id="c1",
        tool_catalog=catalog, events=session._chat_events,
    )

    async def _dispatch():
        return await dispatch_tool(
            name="list_mcp_servers", args={}, ctx=ctx,
            invoker=functools.partial(loop._invoke_router_tool, "list_mcp_servers"),
        )

    dispatch_return = asyncio.run(_dispatch())

    completed = []
    while not outbox.empty():
        msg = outbox.get_nowait()
        if msg.kind == "tool_call_completed":
            completed.append(msg)
    assert completed, "expected a tool_call_completed outbox message"
    conversation_result = completed[-1].meta.get("result")
    conversation_servers = (
        conversation_result.get("servers")
        if isinstance(conversation_result, dict)
        else conversation_result
    )
    conversation_names = {s["name"] for s in (conversation_servers or [])}
    assert conversation_names == {"s1", "s2"}, (
        f"conversation display must show both configured servers, got: {conversation_result}"
    )

    dispatch_return.setdefault("_canonical_source", "list_mcp_servers")
    exec_result = ExecutionResult(
        tool_results=[dispatch_return],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "list_mcp_servers"}}],
        assistant_content="",
    )
    llm_messages = loop.feedback(exec_result)
    tool_message = next(m for m in llm_messages if m["role"] == "tool")

    assert "2 MCP servers" in tool_message["content"], (
        f"LLM-facing text must report the real server count, not 0 — got: "
        f"{tool_message['content']!r}"
    )
    assert "0 MCP servers" not in tool_message["content"]
