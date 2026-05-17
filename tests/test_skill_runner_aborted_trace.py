"""Tier 2: SkillRunner emits ``skill done: aborted`` trace on unexpected exception.

An unexpected Python exception from ``agent.run()`` bypasses the OS-level
``workflow_aborted`` emit, so ``ChatEventForwarder`` never converts the
failure into a ``"skill done: aborted"`` trace. Without that trace the
TUI's ``_handle_trace_for_skill_row`` never calls ``finish_skill_row``,
and the ``SkillActivityRow`` keeps its spinner forever even though an
``ErrorBox`` has already been mounted.

This invariant pins the contract that the outbox carries:
  1. ``kind="error"`` with the failure detail (existing behaviour)
  2. ``kind="trace"`` text ``"skill done: aborted"`` (new — terminates row)
  3. completion enqueue with ``status="error"`` (existing behaviour)

Policy compliance (docs/deep-dives/contributing/testing.md):
  - Real EventLog, real asyncio.Queue, real SkillRunner. No mocks.
  - Observation flows through the public outbox queue + completed-payloads
    list provided by ``_make_runner`` (the existing harness).
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.chat.services.skill_runner import SkillRunner
from reyn.events.events import EventLog

# ── Fakes (mirrors test_skill_runner_invariants.py style) ────────────────────


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


class _RaisingAgent:
    """Agent whose ``run`` raises a non-Cancelled, non-Budget exception."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def run(self, skill: Any, input_artifact: dict, **kwargs) -> Any:
        raise self._exc


def _make_runner_with_raising_agent(exc: Exception):
    events = EventLog()
    outbox: asyncio.Queue = asyncio.Queue()
    completed_payloads: list[dict] = []

    def _build_agent(run_id, skill_name, *, subscribers=None) -> _RaisingAgent:
        return _RaisingAgent(exc)

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
        outbox=outbox,
    )
    return runner, outbox, completed_payloads


# ── Test ──────────────────────────────────────────────────────────────────────


def test_unexpected_exception_emits_skill_done_aborted_trace(tmp_path, monkeypatch):
    """Tier 2: unexpected agent exception enqueues a ``skill done: aborted`` trace.

    The TUI's ``_handle_trace_for_skill_row`` recognises this exact prefix
    and calls ``finish_skill_row`` to stop the spinner. Without it, the
    row spins indefinitely. Verified by draining the outbox and asserting
    both the error message AND the trace are present, in the same
    ``run_id`` meta.
    """
    import reyn.chat.services.skill_runner as sr_mod

    dummy_dir = tmp_path / "fake_skill"
    dummy_dir.mkdir()

    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(sr_mod, "load_dsl_skill", lambda path, *, skill_root: object())

    boom = RuntimeError("unexpected widget crash")
    runner, outbox, completed = _make_runner_with_raising_agent(boom)

    async def _run() -> None:
        await runner.spawn({"skill": "fake", "input": {}})
        # Drive the event loop until the task finishes.
        for _ in range(20):
            if runner.running_names() == []:
                break
            await asyncio.sleep(0)
        assert runner.running_names() == [], "task did not finish"

    asyncio.run(_run())

    # Collect all outbox messages
    msgs = []
    while not outbox.empty():
        msgs.append(outbox.get_nowait())

    # 1. There must be an error message describing the failure
    error_msgs = [m for m in msgs if m.kind == "error"]
    assert error_msgs, f"no error message emitted; got kinds={[m.kind for m in msgs]}"
    assert "unexpected widget crash" in error_msgs[0].text

    # 2. There must be a trace message that terminates the SkillActivityRow
    trace_msgs = [m for m in msgs if m.kind == "trace"]
    aborted_traces = [m for m in trace_msgs if m.text == "skill done: aborted"]
    assert aborted_traces, (
        f"expected 'skill done: aborted' trace; got traces={[m.text for m in trace_msgs]}"
    )

    # 3. Trace meta must carry the same run_id / skill_name as the error
    # so the TUI's _handle_trace_for_skill_row addresses the right row.
    err_meta = error_msgs[0].meta
    trace_meta = aborted_traces[0].meta
    assert trace_meta.get("run_id") == err_meta.get("run_id")
    assert trace_meta.get("skill_name") == err_meta.get("skill_name")

    # 4. Completion still fires (existing invariant — defence against
    # accidentally dropping it while adding the trace)
    assert len(completed) == 1
    assert completed[0]["status"] == "error"
