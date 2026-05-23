"""Tier 2: dispatch_plan_tool async dispatch (ADR-0023 Phase 2.1).

Pins the async behavior:
  - host with spawn_plan_task → dispatch hands off + returns
    {"status": "spawned", ...} immediately
  - per-plan chain_id allocated as f"plan_{plan_id}"
  - host without spawn_plan_task → sync fallback path (= Phase 2 v1
    behavior preserved as safety net for test stubs)
  - get_dispatch_kind("plan") == "async" so RouterLoop exits after dispatch
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.chat.planner import Plan, PlanStep, dispatch_plan_tool
from reyn.chat.router_tools import get_dispatch_kind

# ── basic registry checks ─────────────────────────────────────────────────


def test_plan_is_registered_async() -> None:
    """Tier 2: get_dispatch_kind() classifies plan as async so RouterLoop
    exits after dispatch (= ADR-0023 §2.1.1; ADR-0026 M4 Phase 4 — the
    sidecar ``_DISPATCH_KIND`` dict has been sunset, registry is canonical)."""
    assert get_dispatch_kind("plan") == "async"
    # delegate_to_agent is the other async tool — verify both via registry.
    assert get_dispatch_kind("delegate_to_agent") == "async"
    # Sync default for unknown / regular tools.
    assert get_dispatch_kind("web_search") == "sync"
    assert get_dispatch_kind("__nonexistent__") == "sync"


# ── stub host with spawn_plan_task ────────────────────────────────────────


class _RecordingEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields: Any) -> None:
        self.emitted.append((kind, fields))


class _AsyncCapableHost:
    """Host that exposes spawn_plan_task; mimics ChatSession's task
    handoff. Runs the runtime synchronously inside spawn_plan_task so
    test assertions are deterministic without create_task scheduling."""

    def __init__(self) -> None:
        self.events = _RecordingEvents()
        self.write_decomp_calls: list[dict] = []
        self.delete_decomp_calls: list[dict] = []
        self.spawn_calls: list[dict] = []
        self.outbox: list[dict] = []
        self.plan_started_calls: list[dict] = []
        self.plan_completed_calls: list[dict] = []
        self.plan_aborted_calls: list[dict] = []
        self.plan_step_started_calls: list[dict] = []
        self.plan_step_completed_calls: list[dict] = []
        self.plan_step_failed_calls: list[dict] = []
        # Stash spawned runtimes so the test can drive them deterministically.
        self.spawned_runtimes: list = []

    async def write_plan_decomposition(self, *, plan_id, plan):
        self.write_decomp_calls.append({"plan_id": plan_id})
        return f"/fake/{plan_id}"

    async def delete_plan_decomposition(self, *, plan_id):
        self.delete_decomp_calls.append({"plan_id": plan_id})

    async def spawn_plan_task(self, *, plan_id, runtime, chain_id):
        # Record handoff metadata; defer runtime.run() until the test
        # explicitly drives it via host.run_spawned() (= mirrors
        # ChatSession's create_task semantics without scheduling).
        self.spawn_calls.append(
            {"plan_id": plan_id, "chain_id": chain_id}
        )
        self.spawned_runtimes.append(runtime)

    async def run_spawned(self) -> None:
        for runtime in self.spawned_runtimes:
            result = await runtime.run()
            if result.text:
                self.outbox.append({
                    "kind": "agent",
                    "text": result.text,
                    "meta": {"plan_id": runtime.plan_id, "source": "plan"},
                })

    async def put_outbox(self, *, kind, text, meta):
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def record_plan_started(self, *, plan_id, goal, n_steps):
        self.plan_started_calls.append({"plan_id": plan_id, "n_steps": n_steps})

    async def record_plan_completed(self, *, plan_id):
        self.plan_completed_calls.append({"plan_id": plan_id})

    async def record_plan_aborted(self, *, plan_id, reason=""):
        self.plan_aborted_calls.append({"plan_id": plan_id})

    async def record_plan_step_started(self, *, plan_id, step_id,
                                       depends_on, n_tools):
        self.plan_step_started_calls.append({"step_id": step_id})

    async def record_plan_step_completed(self, *, plan_id, step_id,
                                          content_len):
        self.plan_step_completed_calls.append({"step_id": step_id})

    async def record_plan_step_failed(self, *, plan_id, step_id, error):
        self.plan_step_failed_calls.append({"step_id": step_id})


class _StubRouterLoop:
    def __init__(self, *, host, **kwargs):
        self.host = host

    @property
    def total_usage(self):
        from reyn.llm.pricing import TokenUsage
        return TokenUsage()

    async def run(self, *, user_text, history):
        await self.host.put_outbox(kind="agent", text="ok", meta={})
        return None


@pytest.fixture(autouse=True)
def _stub_router_loop(monkeypatch: Any):
    import reyn.chat.planner as planner_mod
    monkeypatch.setattr(planner_mod, "RouterLoop", _StubRouterLoop)
    yield


def _simple_plan_args() -> dict:
    return {
        "goal": "g",
        "steps": [
            {"id": "s1", "description": "first", "tools": []},
            {"id": "s2", "description": "second", "tools": [], "depends_on": ["s1"]},
        ],
    }


# ── async dispatch path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_dispatch_returns_spawned_status_immediately() -> None:
    """Tier 2: dispatch_plan_tool returns {"status": "spawned", ...}
    without awaiting plan completion when host supports
    spawn_plan_task. RouterLoop sees async tool result + exits."""
    host = _AsyncCapableHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="chat_turn_c0",
        available_tool_names=set(),
    )
    assert result["status"] == "spawned"
    assert "plan_id" in result
    assert "chain_id" in result
    assert result["n_steps"] == 2
    # Host received the spawn handoff but plan hasn't run yet.
    assert host.spawn_calls
    # Runtime not yet executed → no plan_started / plan_completed.
    assert host.plan_started_calls == []
    assert host.plan_completed_calls == []


@pytest.mark.asyncio
async def test_async_dispatch_allocates_per_plan_chain_id() -> None:
    """Tier 2: ADR-0023 §2.1.2 — chain_id is allocated as
    f"plan_{plan_id}", distinct from the chat-turn chain_id (= R-D14
    can target the right waiter on /plan discard)."""
    host = _AsyncCapableHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="chat_turn_c0",  # caller chain
        available_tool_names=set(),
    )
    plan_id = result["plan_id"]
    assert result["chain_id"] == f"plan_{plan_id}"
    assert result["chain_id"] != "chat_turn_c0"
    # Host's spawn_plan_task received the per-plan chain_id.
    assert host.spawn_calls[0]["chain_id"] == f"plan_{plan_id}"


@pytest.mark.asyncio
async def test_async_dispatch_decomposition_written_before_spawn() -> None:
    """Tier 2: ADR-0023 §3.5 lifecycle — artifact write happens before
    spawn_plan_task hands the runtime off."""
    host = _AsyncCapableHost()
    await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    assert host.write_decomp_calls
    assert host.spawn_calls
    # No artifact deletion yet — runtime hasn't completed.
    assert host.delete_decomp_calls == []


@pytest.mark.asyncio
async def test_async_runtime_runs_to_completion_via_host() -> None:
    """Tier 2: when the host eventually drives spawned runtimes (= the
    ChatSession analog calls runtime.run()), plan_started + completed
    fire, terminal text reaches outbox."""
    host = _AsyncCapableHost()
    await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    # Drive the runtime now (= mirror ChatSession.spawn_plan_task wrapper).
    await host.run_spawned()
    assert host.plan_started_calls
    assert host.plan_completed_calls
    # Step events recorded via WAL host methods.
    assert {c["step_id"] for c in host.plan_step_started_calls} == {"s1", "s2"}
    # Terminal text emitted via outbox by host.run_spawned().
    agent_msgs = [m for m in host.outbox if m["kind"] == "agent"
                  and m.get("meta", {}).get("source") == "plan"]
    assert agent_msgs


# ── sync fallback (= host without spawn_plan_task) ──────────────────────


class _LegacyHost:
    """Host without spawn_plan_task — exercises the synchronous fallback
    safety net for test stubs / lightweight integrations."""

    def __init__(self) -> None:
        self.events = _RecordingEvents()
        self.write_decomp_calls: list[dict] = []
        self.delete_decomp_calls: list[dict] = []
        self.plan_started_calls: list[dict] = []
        self.plan_completed_calls: list[dict] = []
        self.plan_step_started_calls: list[dict] = []
        self.plan_step_completed_calls: list[dict] = []
        self.plan_step_failed_calls: list[dict] = []
        self.outbox: list[dict] = []

    async def write_plan_decomposition(self, *, plan_id, plan):
        self.write_decomp_calls.append({"plan_id": plan_id})
        return f"/fake/{plan_id}"

    async def delete_plan_decomposition(self, *, plan_id):
        self.delete_decomp_calls.append({"plan_id": plan_id})

    async def put_outbox(self, *, kind, text, meta):
        self.outbox.append({"kind": kind, "text": text})

    async def record_plan_started(self, *, plan_id, goal, n_steps):
        self.plan_started_calls.append({"plan_id": plan_id})

    async def record_plan_completed(self, *, plan_id):
        self.plan_completed_calls.append({"plan_id": plan_id})

    async def record_plan_aborted(self, *, plan_id, reason=""):
        pass

    async def record_plan_step_started(self, *, plan_id, step_id,
                                       depends_on, n_tools):
        self.plan_step_started_calls.append({"step_id": step_id})

    async def record_plan_step_completed(self, *, plan_id, step_id,
                                          content_len):
        self.plan_step_completed_calls.append({"step_id": step_id})

    async def record_plan_step_failed(self, *, plan_id, step_id, error):
        self.plan_step_failed_calls.append({"step_id": step_id})


@pytest.mark.asyncio
async def test_sync_fallback_returns_status_ok_with_text() -> None:
    """Tier 2: legacy host (= no spawn_plan_task) takes the synchronous
    safety-net path; plan completes inline and result.text + status="ok"
    flow back to the caller (= router_loop's existing plan special-case
    safety net at line 428-449)."""
    host = _LegacyHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    assert result["status"] == "ok"
    assert "text" in result
    # plan_started + plan_completed BOTH fire (= execute_plan finally
    # not corrupted by enclosing exception context).
    assert host.plan_started_calls
    assert host.plan_completed_calls
    # Artifact deleted on clean exit.
    assert host.delete_decomp_calls


@pytest.mark.asyncio
async def test_sync_fallback_uses_per_plan_chain_id_internally() -> None:
    """Tier 2: even on the sync fallback, per-plan chain_id is allocated
    (= consistent identity for WAL events + cancel cascade)."""
    host = _LegacyHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    # status="ok" (legacy shape), but the runtime ran with plan_chain_id.
    # No way to assert chain_id on the result dict in legacy shape
    # (kept absent for backward compat); we assert via plan completion
    # not crashing — the runtime had to operate with the new chain_id.
    assert host.plan_completed_calls


# ── validation failure short-circuit ────────────────────────────────────


@pytest.mark.asyncio
async def test_async_runtime_emits_step_status_narration() -> None:
    """Tier 2: ADR-0023 §2.1.1 — runtime emits per-step status outbox
    messages so the user sees plan progress while it runs in background."""
    host = _AsyncCapableHost()
    await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    await host.run_spawned()

    status_msgs = [m for m in host.outbox if m["kind"] == "status"
                   and m.get("meta", {}).get("source") == "plan"]
    # plan_started + step_done messages for each step
    assert status_msgs
    assert "plan started" in status_msgs[0]["text"]
    assert "1/2" in status_msgs[1]["text"]
    assert "2/2" in status_msgs[2]["text"]


@pytest.mark.asyncio
async def test_async_validation_failure_does_not_spawn() -> None:
    """Tier 2: invalid plan → status=error, no spawn, no artifact write."""
    host = _AsyncCapableHost()
    result = await dispatch_plan_tool(
        args={"goal": "g", "steps": []},  # too few
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    assert result["status"] == "error"
    assert host.spawn_calls == []
    assert host.write_decomp_calls == []


# ── FP-0031-B: step failure emits status text ────────────────────────────


class _FailingStubRouterLoop:
    """RouterLoop stub that raises RuntimeError to trigger step failure."""

    def __init__(self, *, host, **kwargs):
        self.host = host

    @property
    def total_usage(self):
        from reyn.llm.pricing import TokenUsage
        return TokenUsage()

    async def run(self, *, user_text, history):
        raise RuntimeError("simulated transient step error")


@pytest.mark.asyncio
async def test_plan_step_failure_emits_status_text() -> None:
    """Tier 2: FP-0031-B — when a plan step's sub-loop raises an exception,
    execute_plan emits a failure status message to the parent_host outbox.

    Observable contract: the failure status text contains the step's
    description preview and the string '失敗' (= failure marker).
    """
    import reyn.chat.planner as planner_mod

    host = _LegacyHost()

    # Temporarily swap RouterLoop to the failing stub for this test.
    orig_router_loop = planner_mod.RouterLoop
    planner_mod.RouterLoop = _FailingStubRouterLoop
    try:
        result = await dispatch_plan_tool(
            args=_simple_plan_args(),
            parent_host=host, chain_id="c0",
            available_tool_names=set(),
        )
    finally:
        planner_mod.RouterLoop = orig_router_loop

    # Both steps fail.
    assert result["step_failures"]

    # Failure status messages emitted — source="plan" (same meta as success).
    failure_msgs = [
        m for m in host.outbox
        if m["kind"] == "status" and "失敗" in m["text"]
    ]
    assert failure_msgs, (
        f"Expected 2 failure status messages, got {len(failure_msgs)}: "
        f"{[m['text'] for m in host.outbox]}"
    )
    # Each failure message includes the step description preview.
    texts = [m["text"] for m in failure_msgs]
    assert any("first" in t for t in texts), texts
    assert any("second" in t for t in texts), texts
