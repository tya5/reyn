"""Tier 2: SkillRunner emits ``skill done: finished`` trace on success (#1944).

A spawned (background) skill's bottom-strip AsyncStackPanel row is removed by
the TUI's ``_handle_trace_for_skill_row`` when it sees a ``"skill done:"``
trace. The abort/exception branch already enqueues one directly (#106,
``test_skill_runner_aborted_trace``). The **success** branch did NOT — so a
successfully-completing background skill left a ghost row whose elapsed counter
ticked forever (observed in real-terminal e2e: ~2.5 s skill still shown
"running · 1m24s" 90 s later).

This pins the symmetric contract: every non-cancelled terminal branch of
``_run_one_skill`` enqueues a ``"skill done:"`` trace (completeness-by-
construction). The TUI's remove is idempotent, so this is safe even if a
forwarder path also delivers one.

Policy: real EventLog, real asyncio.Queue, real SkillRunner. No mocks.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.core.events.events import EventLog
from reyn.runtime.forwarder import ChatEventForwarder
from reyn.skill.skill_runner import SkillRunner


class _BudgetCheck:
    allowed: bool = True
    hard_dimension = None
    detail = None

    def __init__(self) -> None:
        self.context: dict = {}
        self.warn_dimensions: list = []


class _Budget:
    def check_pre_spawn(self, *, chain_id: str, skill: str) -> _BudgetCheck:
        return _BudgetCheck()

    def record_spawn(self, *, chain_id: str, skill: str) -> None:
        pass

    def extend_chain_calls(self, *, chain_id: str, skill: str, additional: int) -> int:
        return additional


class _Result:
    """Minimal duck-typed successful skill result."""

    def __init__(self) -> None:
        self.status = "completed"
        self.data: dict = {"answer": "ok"}
        self.error = None


class _SucceedingAgent:
    """Agent whose ``run`` returns a normal successful result."""

    async def run(self, skill: Any, input_artifact: dict, **kwargs) -> Any:
        return _Result()


def _make_runner_with_succeeding_agent():
    events = EventLog()
    outbox: asyncio.Queue = asyncio.Queue()
    completed_payloads: list[dict] = []

    def _build_agent(run_id, skill_name, *, subscribers=None) -> _SucceedingAgent:
        return _SucceedingAgent()

    async def _put_outbox(msg) -> None:
        await outbox.put(msg)

    async def _enqueue_completed(*, run_id, skill, chain_id, status, data) -> None:
        completed_payloads.append({
            "run_id": run_id, "skill": skill, "chain_id": chain_id,
            "status": status, "data": data,
        })

    def _accumulate(_result) -> None:
        pass

    def _drop_interventions(_run_id) -> None:
        pass

    def _get_skill_registry():
        return None

    async def _ask_budget_extension(**kwargs) -> bool:
        return False

    runner = SkillRunner(
        event_log=events,
        agent_name="test_agent",
        output_language=None,
        mcp_servers=None,
        allowed_skills=None,
        budget=_Budget(),
        state_log=None,
        build_agent_fn=_build_agent,
        put_outbox=_put_outbox,
        enqueue_skill_completed=_enqueue_completed,
        accumulate=_accumulate,
        drop_interventions_for_run=_drop_interventions,
        get_skill_registry=_get_skill_registry,
        ask_budget_extension=_ask_budget_extension,
        make_subscribers=lambda skill_name, run_id=None: [
            ChatEventForwarder(skill_name, outbox, run_id=run_id),
        ],
        format_refusal=lambda check: "refused",
        format_warn=lambda dim, ctx: "warn",
    )
    return runner, outbox, completed_payloads


def test_successful_skill_emits_skill_done_finished_trace(tmp_path, monkeypatch):
    """Tier 2: a successfully-completing spawned skill enqueues a
    ``skill done: finished`` trace so the TUI removes its async-strip row."""
    import reyn.skill.skill_runner as sr_mod

    dummy_dir = tmp_path / "fake_skill"
    dummy_dir.mkdir()
    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(sr_mod, "load_dsl_skill", lambda path, *, skill_root: object())

    runner, outbox, completed = _make_runner_with_succeeding_agent()

    async def _run() -> None:
        await runner.spawn({"skill": "fake", "input": {}})
        for _ in range(20):
            if runner.running_names() == []:
                break
            await asyncio.sleep(0)
        assert runner.running_names() == [], "task did not finish"

    asyncio.run(_run())

    msgs = []
    while not outbox.empty():
        msgs.append(outbox.get_nowait())

    trace_msgs = [m for m in msgs if m.kind == "trace"]
    finished_traces = [m for m in trace_msgs if m.text == "skill done: finished"]
    assert finished_traces, (
        "expected a 'skill done: finished' trace on successful completion so the "
        f"TUI removes the async-strip row; got traces={[m.text for m in trace_msgs]}"
    )

    # The trace must carry the run_id so the TUI addresses the right strip row.
    (only_completed,) = completed
    assert finished_traces[0].meta.get("run_id") == only_completed["run_id"]
