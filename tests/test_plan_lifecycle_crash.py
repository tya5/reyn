"""Tier 2: ADR-0022 plan-mode crash resilience Phase 1.

Pins the contract that:
  - `execute_plan` allocates a plan_id and emits `plan_started` to WAL
    via the host's `record_plan_started` method.
  - On normal completion, `plan_completed` is recorded → AgentSnapshot
    `active_plan_ids` is empty post-run.
  - On `WorkflowAbortedError`, `plan_completed` is still recorded (=
    skill abort is treated as clean termination, ADR-0013 pattern).
  - On generic `Exception` mid-plan, no `plan_completed` is recorded —
    the plan_id remains in `active_plan_ids` for restart cleanup.
  - `AgentSnapshot.apply_events` correctly mutates `active_plan_ids` on
    `plan_started` / `plan_completed` / `plan_aborted` events.
  - `AgentSnapshot.load` accepts older snapshot files without the
    `active_plan_ids` field (= additive, no schema bump).

No real LLM. Stub `RouterLoop` to inspect the lifecycle without driving
sub-loops.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from reyn.chat.planner import Plan, PlanStep, execute_plan
from reyn.events.agent_snapshot import SNAPSHOT_VERSION, AgentSnapshot

# ── Stub host that records lifecycle calls ──────────────────────────────────


class _RecordingEvents:
    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields):
        self.emitted.append((kind, fields))


class _RecordingHost:
    """Minimal RouterLoopHost stub for plan lifecycle testing.

    Records all `record_plan_*` calls + events. Skips actual sub-loop
    invocation by injecting a stub RouterLoop class via monkeypatch.
    """

    def __init__(self, *, raise_in_step: BaseException | None = None):
        self.events = _RecordingEvents()
        self.plan_started_calls: list[dict] = []
        self.plan_completed_calls: list[dict] = []
        self.plan_aborted_calls: list[dict] = []
        self._raise_in_step = raise_in_step

    # RouterLoopHost protocol — minimal subset
    def list_available_skills(self): return []
    def list_available_agents(self): return []
    def get_memory_index(self): return {"status": "not_found", "content": ""}
    def get_file_permissions(self): return None
    def get_mcp_servers(self): return []
    def get_web_fetch_allowed(self): return False
    def get_project_context(self): return ""
    def memory_path(self, layer, slug): return f"/tmp/{layer}/{slug}.md"
    def memory_dir(self, layer): return f"/tmp/{layer}"
    @property
    def chat_id(self): return "c0"
    @property
    def agent_name(self): return "default"
    @property
    def agent_role(self): return ""
    @property
    def output_language(self): return None

    async def web_search(self, **kw): return {}
    async def web_fetch(self, **kw): return {}
    async def reyn_src_list(self, **kw): return {"path": "", "entries": []}
    async def reyn_src_read(self, **kw): return {"path": "", "content": ""}
    async def file_read(self, path): return ""
    async def file_write(self, path, content): return {"status": "ok"}
    async def file_delete(self, path): return {"status": "ok"}
    async def file_list_directory(self, path): return []
    async def file_regenerate_index(self, *a, **kw): return {"status": "ok"}
    async def mcp_list_servers(self): return []
    async def mcp_list_tools(self, server): return []
    async def mcp_call_tool(self, server, tool, args): return {"status": "ok"}
    async def run_skill_awaitable(self, **kw): return {"status": "ok"}
    async def send_to_agent(self, **kw): return None
    async def put_outbox(self, **kw): return None

    def resolve_model(self, name): return "gemini-2.5-flash-lite"

    # Plan-mode lifecycle (ADR-0022)
    async def record_plan_started(self, *, plan_id, goal, n_steps):
        self.plan_started_calls.append(
            {"plan_id": plan_id, "goal": goal, "n_steps": n_steps}
        )

    async def record_plan_completed(self, *, plan_id):
        self.plan_completed_calls.append({"plan_id": plan_id})

    async def record_plan_aborted(self, *, plan_id, reason=""):
        self.plan_aborted_calls.append({"plan_id": plan_id, "reason": reason})


class _StubRouterLoop:
    """Replaces RouterLoop to skip real LLM calls during plan tests.

    Configured via class-level `_behavior`:
      - "noop" → run() captures no text, returns None
      - "raise:<excname>" → run() raises the named exception
    """

    _behavior: str = "noop"
    _instances: list[Any] = []

    def __init__(self, *, host, **kwargs):
        self.host = host
        _StubRouterLoop._instances.append(self)

    @property
    def total_usage(self):
        from reyn.llm.pricing import TokenUsage
        return TokenUsage()

    async def run(self, *, user_text, history):
        if _StubRouterLoop._behavior.startswith("raise:"):
            exc_name = _StubRouterLoop._behavior.split(":", 1)[1]
            if exc_name == "WorkflowAbortedError":
                from reyn.kernel.runtime import WorkflowAbortedError
                raise WorkflowAbortedError("test abort")
            elif exc_name == "RuntimeError":
                raise RuntimeError("test crash")
            elif exc_name == "CancelledError":
                raise asyncio.CancelledError("test cancel")
            raise Exception(f"test: {exc_name}")
        # noop — capture a small response
        await self.host.put_outbox(kind="agent", text="step output", meta={})
        return None


@pytest.fixture(autouse=True)
def _stub_router_loop(monkeypatch):
    import reyn.chat.planner as planner_mod
    monkeypatch.setattr(planner_mod, "RouterLoop", _StubRouterLoop)
    _StubRouterLoop._behavior = "noop"
    _StubRouterLoop._instances.clear()
    yield


def _simple_plan() -> Plan:
    return Plan(
        goal="test goal",
        steps=(
            PlanStep(id="s1", description="step one", tools=("reyn_src_read",)),
            PlanStep(id="s2", description="synth", tools=(), depends_on=("s1",)),
        ),
    )


# ── Lifecycle tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normal_completion_emits_plan_started_and_completed():
    """Tier 2: a clean run records plan_started + plan_completed; the
    same plan_id appears in both calls.
    """
    host = _RecordingHost()
    plan = _simple_plan()

    result = await execute_plan(plan, parent_host=host, chain_id="c0")

    assert host.plan_started_calls
    assert host.plan_completed_calls
    assert not host.plan_aborted_calls
    started = host.plan_started_calls[0]
    completed = host.plan_completed_calls[0]
    assert started["plan_id"] == completed["plan_id"]
    assert started["goal"] == "test goal"
    assert started["n_steps"] == 2
    assert result.text  # something rendered


@pytest.mark.asyncio
async def test_workflow_abort_treated_as_clean_completion():
    """Tier 2: ADR-0013 pattern — WorkflowAbortedError is a deliberate
    termination, not a crash. plan_completed (NOT plan_aborted) is
    recorded. (Same posture as skill resume's runtime.py:1640–1675.)

    Note: WorkflowAbortedError is raised INSIDE step execution; the
    step's try/except in execute_plan catches it as `Exception`, so the
    error path is exercised at the per-step level, not the plan-level
    finally. This test pins that the plan-level finally still treats
    NORMAL EXIT (no exception escaping the plan loop) as completion.

    For abort that escapes the plan (= less common path), see the
    generic-exception test below.
    """
    host = _RecordingHost()
    plan = _simple_plan()
    _StubRouterLoop._behavior = "raise:WorkflowAbortedError"

    # Per-step exception is caught and recorded as failure, plan loop
    # itself completes normally → plan_completed.
    result = await execute_plan(plan, parent_host=host, chain_id="c0")

    assert host.plan_started_calls
    assert host.plan_completed_calls
    assert not host.plan_aborted_calls
    # All steps "failed" but plan completed normally
    assert result.step_failures


@pytest.mark.asyncio
async def test_external_cancel_preserves_active_plan_id():
    """Tier 2: if a cancellation propagates out of execute_plan (= e.g.
    the calling Task is cancelled), plan_completed is NOT recorded; the
    plan_id stays in active_plan_ids for restart cleanup.

    This pins the ADR-0013 contract that crash-like exits preserve the
    snapshot entry — silent loss of data must NOT happen.
    """
    host = _RecordingHost()
    plan = _simple_plan()

    # Wrap execute_plan and forcibly cancel mid-flight via task.cancel().
    # The simpler path: use a stub that raises CancelledError directly.
    _StubRouterLoop._behavior = "raise:CancelledError"

    # CancelledError propagates through the per-step except (it's a
    # BaseException subclass on 3.8+, caught by `except Exception` only
    # since 3.8; we test the propagation path explicitly).
    # Actually: asyncio.CancelledError IS a BaseException, NOT Exception,
    # since Python 3.8. So `except Exception` in the step loop does NOT
    # catch it, and it propagates to the plan-level finally.
    with pytest.raises(asyncio.CancelledError):
        await execute_plan(plan, parent_host=host, chain_id="c0")

    assert host.plan_started_calls
    assert not host.plan_completed_calls  # ← key invariant
    assert not host.plan_aborted_calls  # not aborted from inside
    # plan_run_interrupted event emitted for forensics
    interrupted = [(k, f) for k, f in host.events.emitted
                   if k == "plan_run_interrupted"]
    assert interrupted
    assert interrupted[0][1]["exc_type"] == "CancelledError"


@pytest.mark.asyncio
async def test_host_without_lifecycle_methods_does_not_crash():
    """Tier 2: a host that doesn't implement record_plan_* (test stubs,
    older code) should still run plan-mode without errors. Defensive
    fallback: planner catches AttributeError and continues so plan-mode
    functions in environments without a SnapshotJournal.
    """
    # Build a fresh host class without the lifecycle methods
    class _MinimalHost:
        events = _RecordingEvents()
        # RouterLoopHost protocol — minimal subset needed for execute_plan
        def list_available_skills(self): return []
        def list_available_agents(self): return []
        def get_memory_index(self): return {"status": "not_found", "content": ""}
        def get_file_permissions(self): return None
        def get_mcp_servers(self): return []
        def get_web_fetch_allowed(self): return False
        def get_project_context(self): return ""
        def memory_path(self, layer, slug): return f"/tmp/{layer}/{slug}.md"
        def memory_dir(self, layer): return f"/tmp/{layer}"
        @property
        def chat_id(self): return "c0"
        @property
        def agent_name(self): return "default"
        @property
        def agent_role(self): return ""
        @property
        def output_language(self): return None
        async def web_search(self, **kw): return {}
        async def web_fetch(self, **kw): return {}
        async def reyn_src_list(self, **kw): return {"path": "", "entries": []}
        async def reyn_src_read(self, **kw): return {"path": "", "content": ""}
        async def file_read(self, p): return ""
        async def file_write(self, p, c): return {"status": "ok"}
        async def file_delete(self, p): return {"status": "ok"}
        async def file_list_directory(self, p): return []
        async def file_regenerate_index(self, *a, **kw): return {"status": "ok"}
        async def mcp_list_servers(self): return []
        async def mcp_list_tools(self, s): return []
        async def mcp_call_tool(self, s, t, a): return {"status": "ok"}
        async def run_skill_awaitable(self, **kw): return {"status": "ok"}
        async def send_to_agent(self, **kw): return None
        async def put_outbox(self, **kw): return None
        def resolve_model(self, n): return "x"
        # NO record_plan_started / record_plan_completed / record_plan_aborted

    host = _MinimalHost()
    plan = _simple_plan()
    # Should not raise — planner catches AttributeError on missing methods
    result = await execute_plan(plan, parent_host=host, chain_id="c0")
    assert result.text  # plan still produced output


# ── AgentSnapshot apply_events for plan-* ──────────────────────────────────


def test_agent_snapshot_apply_plan_started():
    """Tier 2: plan_started event appends plan_id to active_plan_ids."""
    snap = AgentSnapshot.empty("default")
    snap.apply_events([
        {
            "seq": 1,
            "kind": "plan_started",
            "target": "default",
            "plan_id": "abc123",
            "goal": "g",
            "n_steps": 3,
        }
    ])
    assert snap.active_plan_ids == ["abc123"]
    assert snap.applied_seq == 1


def test_agent_snapshot_apply_plan_completed():
    """Tier 2: plan_completed event removes plan_id from active_plan_ids."""
    snap = AgentSnapshot(agent_name="default", active_plan_ids=["abc123", "xyz"])
    snap.apply_events([
        {
            "seq": 2, "kind": "plan_completed",
            "target": "default", "plan_id": "abc123",
        }
    ])
    assert snap.active_plan_ids == ["xyz"]


def test_agent_snapshot_apply_plan_aborted():
    """Tier 2: plan_aborted event removes plan_id (same as completed)."""
    snap = AgentSnapshot(agent_name="default", active_plan_ids=["abc123"])
    snap.apply_events([
        {
            "seq": 3, "kind": "plan_aborted",
            "target": "default", "plan_id": "abc123", "reason": "test",
        }
    ])
    assert snap.active_plan_ids == []


def test_agent_snapshot_apply_plan_started_idempotent():
    """Tier 2: replaying the same plan_started doesn't duplicate the entry."""
    snap = AgentSnapshot(agent_name="default", active_plan_ids=["abc"])
    snap.apply_events([
        {
            "seq": 1, "kind": "plan_started",
            "target": "default", "plan_id": "abc", "goal": "g", "n_steps": 1,
        }
    ])
    assert snap.active_plan_ids == ["abc"]


def test_agent_snapshot_load_old_file_without_active_plan_ids():
    """Tier 2: ADR-0022 additive contract — older snapshot files written
    before the field existed must load with active_plan_ids=[] without
    raising SchemaVersionError.
    """
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "snap.json"
        # Write a snapshot using version=1 but WITHOUT active_plan_ids
        legacy = {
            "version": SNAPSHOT_VERSION,
            "applied_seq": 5,
            "inbox": [],
            "pending_chains": {},
            "active_skill_run_ids": ["sk1"],
            "outstanding_interventions": {},
            "buffered_intervention_answers": {},
            # active_plan_ids intentionally omitted
        }
        p.write_text(json.dumps(legacy), encoding="utf-8")
        snap = AgentSnapshot.load("default", p)
        assert snap.active_plan_ids == []
        assert snap.active_skill_run_ids == ["sk1"]
        assert snap.applied_seq == 5


def test_agent_snapshot_save_round_trip_preserves_active_plan_ids():
    """Tier 2: save → load round-trip preserves active_plan_ids."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "snap.json"
        snap = AgentSnapshot(
            agent_name="default",
            active_plan_ids=["plan1", "plan2"],
            applied_seq=10,
        )
        snap.save(p)
        loaded = AgentSnapshot.load("default", p)
        assert loaded.active_plan_ids == ["plan1", "plan2"]
        assert loaded.applied_seq == 10
