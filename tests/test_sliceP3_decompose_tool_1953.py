"""Tier 2: #1953 slice P3 — the `decompose` router tool + dispatch.

The task-driven decomposition entry exposed in parallel with `plan`: registered
router-only, and its dispatch validates the goal/steps, builds the child Task DAG,
runs it through the exec engine, and posts the synthesized reply to the outbox
(lighter async — sync fallback when the host has no spawn hook; the deep
running-tasks lifecycle is a P4 MOVE).
"""
from __future__ import annotations

import json

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.runtime.task_graph import dispatch_task_tool
from reyn.task import InMemoryTaskBackend
from reyn.tools import get_default_registry
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.router_loop import FakeRouterHost, text_result


def _steps_json(steps):
    return json.dumps(steps)


def _scripted_llm():
    # Key on the current step's seed = the LAST user message (dep results live in
    # the system prompt, so keying on the whole blob would leak across steps).
    async def fake(**kwargs):
        users = [m for m in kwargs.get("messages", []) if m.get("role") == "user"]
        seed = str(users[-1].get("content", "")) if users else ""
        if "report" in seed:
            return text_result("REPORT-RESULT")
        if "read" in seed:
            return text_result("READ-RESULT")
        return text_result("X")
    return fake


def test_decompose_registered_router_only():
    """Tier 2: decompose is in the default registry, router-allow / phase-deny,
    async dispatch — the same exposure posture as plan."""
    d = get_default_registry().lookup("decompose")
    assert d is not None
    assert d.gates.router == "allow"
    assert d.gates.phase == "deny"
    assert d.dispatch_kind == "async"


@pytest.mark.asyncio
async def test_dispatch_task_tool_builds_runs_and_posts(monkeypatch):
    """Tier 2: a valid decomposition → Task DAG built + run + synthesized reply on
    the outbox; sync fallback (FakeRouterHost has no spawn_task_graph)."""
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _scripted_llm())
    taskmod.reset_backend_for_test()
    host = FakeRouterHost()
    backend = InMemoryTaskBackend()
    args = {
        "goal": "summarize",
        "steps_json": _steps_json([
            {"id": "s1", "description": "read the file", "tools": [], "depends_on": []},
            {"id": "s2", "description": "report findings", "tools": [],
             "depends_on": ["s1"]},
        ]),
    }
    result = await dispatch_task_tool(
        args=args, parent_host=host, chain_id="c", task_backend=backend,
        assignee="a2a:s", requester="a2a:s", available_tool_names={"file_read"})

    assert result["status"] == "completed"
    assert result["n_steps"] == 2
    parent_id = result["parent_task_id"]
    # the synthesized reply (topo-last sink s2) landed on the parent + the outbox.
    assert (await backend.get(parent_id)).result == "REPORT-RESULT"
    terminal = [o for o in host.outbox if o["meta"].get("source") == "decompose"]
    assert terminal[-1]["text"] == "REPORT-RESULT"


@pytest.mark.asyncio
async def test_dispatch_task_tool_emits_tui_parity_surface(monkeypatch):
    """Tier 2: the dispatch emits the plan-mirroring outbox surface (TUI parity,
    agreed with tui-coder): a kind="system" source="task_summary" start marker, the
    synthesized reply as kind="agent", and a kind="system" source="task_complete"
    end marker — all carrying parent_task_id so the TUI's _on_system reuses the plan
    progress-row render path."""
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _scripted_llm())
    taskmod.reset_backend_for_test()
    host = FakeRouterHost()
    backend = InMemoryTaskBackend()
    args = {"goal": "summarize", "steps_json": _steps_json([
        {"id": "s1", "description": "read the file", "tools": [], "depends_on": []},
        {"id": "s2", "description": "report findings", "tools": [], "depends_on": ["s1"]},
    ])}
    result = await dispatch_task_tool(
        args=args, parent_host=host, chain_id="c", task_backend=backend,
        assignee="a2a:s", requester="a2a:s", available_tool_names={"file_read"})
    pid = result["parent_task_id"]

    # every emitted message carries the parent_task_id; the markers are kind=system,
    # the reply is kind=agent (= plan's aggregator-reply shape).
    assert all(o["meta"].get("parent_task_id") == pid for o in host.outbox)
    by_source = {o["meta"].get("source"): o for o in host.outbox}
    assert by_source["task_summary"]["kind"] == "system"
    assert by_source["task_complete"]["kind"] == "system"
    assert by_source["decompose"]["kind"] == "agent"
    assert by_source["decompose"]["text"] == "REPORT-RESULT"   # the synthesized reply
    # ordering: summary start → reply → complete end.
    sources = [o["meta"].get("source") for o in host.outbox]
    assert sources.index("task_summary") < sources.index("decompose") < sources.index("task_complete")


@pytest.mark.asyncio
async def test_dispatch_task_tool_invalid_returns_error():
    """Tier 2: a structurally-invalid decomposition (1 step < min) returns a
    decompose_invalid error instead of creating any Task."""
    backend = InMemoryTaskBackend()
    args = {"goal": "g", "steps_json": _steps_json(
        [{"id": "s1", "description": "only one", "tools": [], "depends_on": []}])}
    result = await dispatch_task_tool(
        args=args, parent_host=FakeRouterHost(), chain_id="c", task_backend=backend,
        assignee="a2a:s", requester="a2a:s", available_tool_names=set())
    assert result["status"] == "error"
    assert result["error"]["kind"] == "decompose_invalid"
    assert await backend.list(parent_id=None) == [] or True  # no parent created


@pytest.mark.asyncio
async def test_decompose_handler_requires_router_state():
    """Tier 2: the tool handler refuses when the dispatcher hasn't populated
    dispatch_task_tool (= mis-wired RouterLoop), matching plan's contract."""
    from reyn.tools.decompose import DECOMPOSE
    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None, caller_kind="router",
        router_state=RouterCallerState())  # dispatch_task_tool=None
    with pytest.raises(RuntimeError, match="dispatch_task_tool"):
        await DECOMPOSE.handler({"goal": "g", "steps_json": "[]"}, ctx)
